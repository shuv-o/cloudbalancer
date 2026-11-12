#!/bin/sh
#
# Runs `nginx -t` and `nginx -s reload` on behalf of the control plane,
# and rotates the access logs.
#
# The management container used to do this itself with
# `docker exec cloudbalancer-nginx nginx -s reload`, which meant mounting
# /var/run/docker.sock into it. That socket is the Docker API with no
# authorisation on it: anything that can write to it can start a container
# with the host's filesystem mounted, so a bug anywhere in the panel -- a
# template injection, a dependency with a flaw, a stolen session -- was root
# on the host rather than a compromised web application.
#
# The whole of what the panel needed was these two commands. So instead of a
# general-purpose remote-execution channel, it gets a directory: it writes the
# name of one of a few commands into a file, and this answers. Nothing here
# executes anything the file says; the file selects between fixed commands and
# anything else is refused.
#
# Rotation lives here rather than in the control plane for two reasons. The
# files belong to this container -- the management container mounts them
# read-only, so it could not rename one if it wanted to -- and, more to the
# point, unbounded log growth must not depend on the control plane being up.
# It has been down for hours at a time. Disks do not wait.
#
# The protocol is deliberately not JSON. `nginx -t` emits quotes, colons and
# newlines, and escaping those correctly in POSIX shell is a worse problem
# than the one being solved. A result file is an exit code on the first line
# and the raw output after it.
#
set -u

CONTROL="${CLOUDBALANCER_CONTROL_DIR:-/var/run/cloudbalancer}"
REQUESTS="$CONTROL/requests"
RESULTS="$CONTROL/results"

# Results nobody collected. The control plane deletes the result it asked for,
# so anything older than this belongs to a request whose caller timed out or
# restarted, and would otherwise accumulate for the life of the container.
STALE_MINUTES="${CLOUDBALANCER_RESULT_TTL_MINUTES:-10}"

# Rotation. Size-triggered rather than daily: a log is a problem when it is
# large, not when it is old, and a gateway serving six quiet domains would
# otherwise accumulate a year of near-empty files to no purpose.
LOG_DIR="${CLOUDBALANCER_LOG_DIR:-/var/log/nginx}"
ROTATE_BYTES="${CLOUDBALANCER_LOG_ROTATE_BYTES:-67108864}"
ROTATE_KEEP="${CLOUDBALANCER_LOG_KEEP:-7}"
# How many passes of the one-second loop between size checks.
ROTATE_EVERY="${CLOUDBALANCER_LOG_CHECK_SECONDS:-60}"
# The shortest gap between two rotations of the same file.
#
# Only .1 is left readable; a second rotation compresses it to .2, and the
# control plane reads .1 on a timer of its own. So two rotations inside one
# of its passes would bury lines it has not read yet -- recoverable from a
# .gz by hand, but gone from the counts. Holding a file open a little past
# its threshold is the cheaper mistake, and this bounds the overshoot to one
# interval's worth of traffic rather than letting it run.
ROTATE_MIN_INTERVAL="${CLOUDBALANCER_LOG_MIN_INTERVAL_SECONDS:-900}"

mkdir -p "$REQUESTS" "$RESULTS"

log() {
    echo "[reload-watcher] $*" >&2
}

write_result() {
    id="$1"
    code="$2"
    output="$3"

    # Written elsewhere and renamed into place: a reader polling this
    # directory must never see half a result and conclude the command failed.
    tmp="$RESULTS/.$id.partial"
    {
        echo "$code"
        printf '%s\n' "$output"
    } > "$tmp"
    mv "$tmp" "$RESULTS/$id"
}

# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

file_size() {
    # BusyBox and GNU take -c, BSD takes -f. The gateway image is Alpine, so
    # only the first branch runs in production -- but the test for this runs
    # on whatever a developer has, and a size of 0 everywhere would have made
    # it pass by never rotating anything.
    stat -c %s "$1" 2>/dev/null \
        || stat -f %z "$1" 2>/dev/null \
        || wc -c < "$1" 2>/dev/null \
        || echo 0
}

# Move one file down the numbered chain and take its name for a new one.
#
# .1 is left uncompressed on purpose. The control plane reads it: when a
# rename happens between two of its passes, the tail of the file it was
# following is in .1 and nowhere else, and it recovers those lines from
# there. Compressing .1 would hide them until the next rotation. Everything
# from .2 down is history for a human with zgrep, so it is compressed.
# POSIX sh has no local scope, and these functions are called from a loop
# that has variables of its own, so every name here is prefixed. An earlier
# version used a bare `i` and silently reset the counter of whatever called
# it -- which, in the one place that happened, was an infinite loop.
rotate_one() {
    _rot_file="$1"

    rm -f "$_rot_file.$ROTATE_KEEP" "$_rot_file.$ROTATE_KEEP.gz"

    _rot_i="$ROTATE_KEEP"
    while [ "$_rot_i" -gt 2 ]; do
        _rot_prev=$((_rot_i - 1))
        if [ -f "$_rot_file.$_rot_prev.gz" ]; then
            mv -f "$_rot_file.$_rot_prev.gz" "$_rot_file.$_rot_i.gz"
        fi
        # A leftover from a gzip that was interrupted by a restart.
        if [ -f "$_rot_file.$_rot_prev" ]; then
            mv -f "$_rot_file.$_rot_prev" "$_rot_file.$_rot_i"
        fi
        _rot_i="$_rot_prev"
    done

    if [ -f "$_rot_file.1" ]; then
        mv -f "$_rot_file.1" "$_rot_file.2"
        gzip -f "$_rot_file.2" 2>/dev/null || true
    fi

    mv -f "$_rot_file" "$_rot_file.1"
}

# Returns 0 if it rotated anything. `force` ignores the size threshold.
rotate_logs() {
    _rot_mode="${1:-size}"
    _rot_done=""

    for _rot_candidate in "$LOG_DIR"/*.log; do
        [ -f "$_rot_candidate" ] || continue

        if [ "$_rot_mode" != "force" ]; then
            [ "$(file_size "$_rot_candidate")" -ge "$ROTATE_BYTES" ] || continue

            # find -newermt is not in BusyBox. -mmin is, and minutes are
            # resolution enough for something measured in a quarter of an
            # hour.
            if [ "$ROTATE_MIN_INTERVAL" -gt 0 ] && [ -f "$_rot_candidate.1" ]; then
                _rot_mins=$(( (ROTATE_MIN_INTERVAL + 59) / 60 ))
                if [ -z "$(find "$_rot_candidate.1" -mmin "+$_rot_mins" 2>/dev/null)" ]; then
                    log "$(basename "$_rot_candidate") is over the threshold, but its .1 is younger than ${_rot_mins}m and may not have been read yet; waiting"
                    continue
                fi
            fi
        fi

        rotate_one "$_rot_candidate"
        _rot_done="$_rot_done $(basename "$_rot_candidate")"
    done

    if [ -z "$_rot_done" ]; then
        echo "nothing over $ROTATE_BYTES bytes"
        return 1
    fi

    # Renaming a file does not move the descriptor Nginx is writing through.
    # Until it reopens, every line still goes into the file now called .1
    # while the new one stays empty -- rotation that appears to work, frees
    # nothing, and is only noticed when the disk fills.
    _rot_reopen="$(nginx -s reopen 2>&1)"
    echo "rotated:$_rot_done"
    if [ -n "$_rot_reopen" ]; then
        echo "$_rot_reopen"
    fi
    log "rotated$_rot_done"
    return 0
}

# ---------------------------------------------------------------------------
# The commands
# ---------------------------------------------------------------------------

# The whole of what this accepts. A name that is not one of these selects
# nothing and is refused -- the string is never passed to a shell, so there
# is no quoting to get wrong and nothing to inject into.
run_command() {
    case "$1" in
        test)
            nginx -t 2>&1
            return $?
            ;;
        reload)
            # Tested first. `nginx -s reload` on a broken configuration
            # leaves the running workers alone and reports success from the
            # signal, so without this a bad deploy looks like a good one
            # until the next restart -- which might be months later,
            # unattended, and after whoever changed it has forgotten.
            tested="$(nginx -t 2>&1)"
            code=$?
            if [ "$code" -ne 0 ]; then
                printf '%s\n' "$tested"
                return "$code"
            fi
            reloaded="$(nginx -s reload 2>&1)"
            code=$?
            printf '%s\n%s\n' "$tested" "$reloaded"
            return "$code"
            ;;
        rotate)
            # Forced, because asking for a rotation and being told the files
            # were too small is not what the caller meant.
            rotate_logs force 2>&1
            return $?
            ;;
        *)
            echo "refused: '$1' is not a command this accepts"
            log "refused: '$1' is not a command this accepts"
            return 127
            ;;
    esac
}

# A test sources this file to exercise the rotation against real files, and
# would otherwise be hung by the loop below. Everything above this line is
# definitions; nothing has run.
if [ "${CLOUDBALANCER_WATCHER_SOURCED:-}" = "1" ]; then
    return 0
fi

ticks=0

log "watching $REQUESTS; rotating $LOG_DIR/*.log above $ROTATE_BYTES bytes, keeping $ROTATE_KEEP"

while :; do
    for request in "$REQUESTS"/*; do
        [ -f "$request" ] || continue

        id="$(basename "$request")"
        command="$(cat "$request" 2>/dev/null)"
        # Removed before running, not after. A command that wedges the
        # watcher must not be retried forever on every pass.
        rm -f "$request"

        output="$(run_command "$command")"
        code=$?

        write_result "$id" "$code" "$output"
    done

    find "$RESULTS" -type f -mmin "+$STALE_MINUTES" -delete 2>/dev/null

    sleep 1

    ticks=$((ticks + 1))
    if [ "$ROTATE_EVERY" -gt 0 ] && [ "$ticks" -ge "$ROTATE_EVERY" ]; then
        ticks=0
        rotate_logs >/dev/null 2>&1
    fi
done
