#!/bin/sh
#
# Exercises the rotation in reload-watcher.sh against real files.
#
# Worth having because the failure it guards against is silent and slow. A
# rotation that renames but never reopens looks correct from the outside --
# the new file is there, the old one is numbered -- while Nginx keeps writing
# through the descriptor it already had, so the "rotated" file goes on
# growing and nothing is ever freed. That is noticed when the disk fills,
# typically at night.
#
# The script sources the watcher, which stops short of its main loop when
# CLOUDBALANCER_WATCHER_SOURCED is set, and stubs `nginx` so the reopen can
# be observed rather than performed.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

FAILURES=0

ok() {
    printf '  ok   %s\n' "$1"
}

fail() {
    printf '  FAIL %s\n     %s\n' "$1" "$2"
    FAILURES=$((FAILURES + 1))
}

check() {
    if [ "$2" = "$3" ]; then
        ok "$1"
    else
        fail "$1" "expected '$3', got '$2'"
    fi
}

exists() {
    if [ -e "$2" ]; then ok "$1"; else fail "$1" "$2 does not exist"; fi
}

absent() {
    if [ -e "$2" ]; then fail "$1" "$2 should not exist"; else ok "$1"; fi
}

# A stand-in for Nginx that records what it was asked to do.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/nginx" <<'STUB'
#!/bin/sh
echo "$@" >> "$NGINX_CALLS"
STUB
chmod +x "$WORK/bin/nginx"
PATH="$WORK/bin:$PATH"
export PATH
NGINX_CALLS="$WORK/nginx-calls"
export NGINX_CALLS
: > "$NGINX_CALLS"

LOGS="$WORK/logs"
mkdir -p "$LOGS"

CLOUDBALANCER_CONTROL_DIR="$WORK/control"
CLOUDBALANCER_LOG_DIR="$LOGS"
CLOUDBALANCER_LOG_ROTATE_BYTES=1024
CLOUDBALANCER_LOG_KEEP=4
# Off for most of the checks below, which rotate the same file repeatedly in
# well under a second. It has a check of its own at the end.
CLOUDBALANCER_LOG_MIN_INTERVAL_SECONDS=0
# Daily rotation is driven by a stamp file; the checks below drive it by hand.
CLOUDBALANCER_LOG_MAX_AGE_MINUTES=1440
export CLOUDBALANCER_CONTROL_DIR CLOUDBALANCER_LOG_DIR
export CLOUDBALANCER_LOG_ROTATE_BYTES CLOUDBALANCER_LOG_KEEP
export CLOUDBALANCER_LOG_MIN_INTERVAL_SECONDS CLOUDBALANCER_LOG_MAX_AGE_MINUTES

CLOUDBALANCER_WATCHER_SOURCED=1
export CLOUDBALANCER_WATCHER_SOURCED
# shellcheck source=/dev/null
. "$HERE/reload-watcher.sh"

big() {
    # Comfortably over the 1024-byte threshold set above.
    i=0
    : > "$1"
    while [ "$i" -lt 40 ]; do
        echo "203.0.113.7 - - [12/Nov/2026:10:15:00 +0000] line $i" >> "$1"
        i=$((i + 1))
    done
}

echo "rotation"

# --- a file under the threshold is left alone ------------------------------
echo "small" > "$LOGS/quiet.example.com.access.log"
rotate_logs > /dev/null 2>&1
absent "a small log is not rotated" "$LOGS/quiet.example.com.access.log.1"
check "nothing was reopened" "$(wc -l < "$NGINX_CALLS" | tr -d ' ')" "0"

# --- a file over it is renamed and Nginx is told ---------------------------
big "$LOGS/busy.example.com.access.log"
rotate_logs > /dev/null 2>&1
exists "a large log is rotated to .1" "$LOGS/busy.example.com.access.log.1"
absent "the live name is free for a new file" "$LOGS/busy.example.com.access.log"
check "Nginx was told to reopen" "$(cat "$NGINX_CALLS")" "-s reopen"
check "the small log beside it is untouched" \
    "$(cat "$LOGS/quiet.example.com.access.log")" "small"

# --- .1 stays readable, .2 onward is compressed ----------------------------
big "$LOGS/busy.example.com.access.log"
rotate_logs > /dev/null 2>&1
exists "the newest rotation is plain text" "$LOGS/busy.example.com.access.log.1"
exists "the one before it is compressed" "$LOGS/busy.example.com.access.log.2.gz"
absent "and is not left uncompressed" "$LOGS/busy.example.com.access.log.2"

# --- the chain shifts and stops at KEEP ------------------------------------
chain=0
while [ "$chain" -lt 6 ]; do
    big "$LOGS/busy.example.com.access.log"
    rotate_logs > /dev/null 2>&1
    chain=$((chain + 1))
done
exists "the chain reaches KEEP" "$LOGS/busy.example.com.access.log.4.gz"
absent "and does not grow past it" "$LOGS/busy.example.com.access.log.5.gz"
absent "nor past it uncompressed" "$LOGS/busy.example.com.access.log.5"

# --- error logs rotate too -------------------------------------------------
big "$LOGS/error.log"
rotate_logs > /dev/null 2>&1
exists "error.log rotates as well" "$LOGS/error.log.1"

# --- force ignores the threshold -------------------------------------------
echo "tiny" > "$LOGS/quiet.example.com.access.log"
rotate_logs force > /dev/null 2>&1
exists "a forced rotation ignores the size" "$LOGS/quiet.example.com.access.log.1"

# --- an already-rotated file is not rotated again --------------------------
: > "$NGINX_CALLS"
rotate_logs > /dev/null 2>&1
absent "a .1 is never itself rotated" "$LOGS/busy.example.com.access.log.1.1"
check "a pass with nothing to do does not reopen" \
    "$(wc -l < "$NGINX_CALLS" | tr -d ' ')" "0"

# --- a fresh .1 holds off the next rotation --------------------------------
# Only .1 is readable; rotating again compresses it. If that happens before
# the control plane's next pass, the lines in it never reach the counts.
ROTATE_MIN_INTERVAL=900
rm -f "$LOGS"/throttle.example.com.access.log*
big "$LOGS/throttle.example.com.access.log"
rotate_logs > /dev/null 2>&1
exists "the first rotation happens" "$LOGS/throttle.example.com.access.log.1"
big "$LOGS/throttle.example.com.access.log"
rotate_logs > /dev/null 2>&1
absent "a second rotation within the interval is held off" \
    "$LOGS/throttle.example.com.access.log.2.gz"
exists "and the live file is left to grow a little" \
    "$LOGS/throttle.example.com.access.log"

# Backdating .1 is the same thing as the interval having passed.
touch -t 200001010000 "$LOGS/throttle.example.com.access.log.1"
rotate_logs > /dev/null 2>&1
exists "once the interval has passed it rotates" \
    "$LOGS/throttle.example.com.access.log.2.gz"

# A human asking for one is not throttled.
big "$LOGS/throttle.example.com.access.log"
rotate_logs force > /dev/null 2>&1
exists "a forced rotation ignores the interval" \
    "$LOGS/throttle.example.com.access.log.3.gz"
ROTATE_MIN_INTERVAL=0

echo "daily rotation"

# --- the stamp decides, not any log's mtime --------------------------------
rm -f "$LOGS/.last-rotation"
if rotation_due_by_age; then
    fail "a first run creates the stamp rather than rotating" "it said a rotation was due"
else
    ok "a first run creates the stamp rather than rotating"
fi
exists "and the stamp is there afterwards" "$LOGS/.last-rotation"

if rotation_due_by_age; then
    fail "a fresh stamp means nothing is due" "it said a rotation was due"
else
    ok "a fresh stamp means nothing is due"
fi

touch -t 200001010000 "$LOGS/.last-rotation"
if rotation_due_by_age; then
    ok "an old stamp means a daily rotation is due"
else
    fail "an old stamp means a daily rotation is due" "it said nothing was due"
fi

# --- a daily rotation takes small files that the size trigger would skip ---
rm -f "$LOGS"/daily.example.com.access.log*
echo "one line, far under the threshold" > "$LOGS/daily.example.com.access.log"
rotate_logs size > /dev/null 2>&1
absent "the size trigger leaves a small log alone" "$LOGS/daily.example.com.access.log.1"
rotate_logs age > /dev/null 2>&1
exists "the daily one takes it" "$LOGS/daily.example.com.access.log.1"

# --- but not an empty one ---------------------------------------------------
rm -f "$LOGS"/silent.example.com.access.log*
: > "$LOGS/silent.example.com.access.log"
rotate_logs age > /dev/null 2>&1
absent "an empty log is not rotated daily into a chain of nothing" \
    "$LOGS/silent.example.com.access.log.1"

# --- the container's own stdout/stderr are symlinks and must not move -------
ln -sf /dev/stdout "$LOGS/stdout.log"
rotate_logs force > /dev/null 2>&1
absent "a symlinked log is never rotated" "$LOGS/stdout.log.1"
if [ -L "$LOGS/stdout.log" ]; then
    ok "and the symlink itself is intact"
else
    fail "and the symlink itself is intact" "it is no longer a symlink"
fi
rm -f "$LOGS/stdout.log"

echo "commands"

# --- the dispatch accepts three names and refuses everything else ----------
: > "$NGINX_CALLS"
out="$(run_command test)"
check "test runs a config check" "$(cat "$NGINX_CALLS")" "-t"

: > "$NGINX_CALLS"
out="$(run_command reload)"
check "reload checks before signalling" "$(tr '\n' ' ' < "$NGINX_CALLS")" "-t -s reload "

: > "$NGINX_CALLS"
big "$LOGS/busy.example.com.access.log"
out="$(run_command rotate)"
exists "rotate rotates regardless of size" "$LOGS/busy.example.com.access.log.1"

# Anything else must select no command at all. These are the shapes an
# injection would take if the request file were ever handed to a shell: it
# is not, and this says so.
for word in "rm -rf /" "rotate; id" "reload && cat /etc/shadow" "reopen" \
            "TEST" "test " "" "\$(id)" "../../bin/sh"; do
    : > "$NGINX_CALLS"
    out="$(run_command "$word")"
    code=$?
    if [ "$code" -ne 127 ]; then
        fail "'$word' is refused" "exit $code, expected 127"
    elif [ -s "$NGINX_CALLS" ]; then
        fail "'$word' is refused" "it reached nginx: $(cat "$NGINX_CALLS")"
    else
        ok "'$word' is refused"
    fi
done

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "rotation: all checks passed"
    exit 0
fi
echo "rotation: $FAILURES check(s) failed"
exit 1
