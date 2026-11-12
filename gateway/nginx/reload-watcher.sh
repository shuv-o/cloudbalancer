#!/bin/sh
#
# Runs `nginx -t` and `nginx -s reload` on behalf of the control plane.
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
# name of one of two commands into a file, and this answers. Nothing here
# executes anything the file says; the file selects between two fixed
# commands and anything else is refused.
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

log "watching $REQUESTS"

while :; do
    for request in "$REQUESTS"/*; do
        [ -f "$request" ] || continue

        id="$(basename "$request")"
        command="$(cat "$request" 2>/dev/null)"
        # Removed before running, not after. A command that wedges the
        # watcher must not be retried forever on every pass.
        rm -f "$request"

        case "$command" in
            test)
                output="$(nginx -t 2>&1)"
                code=$?
                ;;
            reload)
                # Tested first. `nginx -s reload` on a broken configuration
                # leaves the running workers alone and reports success from
                # the signal, so without this a bad deploy looks like a good
                # one until the next restart -- which might be months later,
                # unattended, and after whoever changed it has forgotten.
                output="$(nginx -t 2>&1)"
                code=$?
                if [ "$code" -eq 0 ]; then
                    reload_output="$(nginx -s reload 2>&1)"
                    code=$?
                    output="$output
$reload_output"
                fi
                ;;
            *)
                output="refused: '$command' is not a command this accepts"
                code=127
                log "$output"
                ;;
        esac

        write_result "$id" "$code" "$output"
    done

    find "$RESULTS" -type f -mmin "+$STALE_MINUTES" -delete 2>/dev/null

    sleep 1
done
