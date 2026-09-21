#!/usr/bin/env bash
# =============================================================================
# The gateway's own overhead, straight out of the access log.
#
# Nginx records total request time and upstream response time separately, so
# their difference is what the proxy itself added -- per request, on real
# traffic, with no load generator and no second machine involved. When the
# external benchmark and this disagree, this is the one measuring the right
# thing.
#
#   ./added_latency_from_logs.sh [logfile]
# =============================================================================
set -euo pipefail

LOG="${1:-/var/log/nginx/access.log}"
CONTAINER="${NGINX_CONTAINER:-proxy-balancer-nginx}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

read_log() {
    if [[ -r "$LOG" ]]; then
        cat "$LOG"
    else
        docker exec "$CONTAINER" cat "$LOG"
    fi
}

# Pull rt/urt/uct out of each line. Requests that never reached an upstream
# (cache hits, redirects, 404s) have no upstream time and are skipped: they
# are not proxied requests, so there is no proxy overhead to attribute.
read_log | awk '
    {
        rt = -1; urt = -1; uct = -1
        for (i = 1; i <= NF; i++) {
            if ($i ~ /^rt=/)  { rt = substr($i, 4) }
            if ($i ~ /^urt=/) { split(substr($i, 5), a, ","); urt = a[1] }
            if ($i ~ /^uct=/) { split(substr($i, 5), b, ","); uct = b[1] }
        }
        if (rt == -1 || urt == "-" || urt == -1) next
        handshake = (uct != "-" && uct + 0 > 0) ? 1 : 0
        printf "%.6f %d\n", (rt - urt) * 1000, handshake
    }
' > "$TMP/samples"

if [[ ! -s "$TMP/samples" ]]; then
    echo "No proxied requests with timing in $LOG yet. Send some traffic first."
    exit 1
fi

handshakes=$(awk '{s += $2} END {print s + 0}' "$TMP/samples")
total=$(wc -l < "$TMP/samples" | tr -d ' ')

# sort -n rather than awk's asort, which only exists in gawk.
cut -d' ' -f1 "$TMP/samples" | sort -n > "$TMP/sorted"

pct() {
    awk -v p="$1" -v n="$total" 'NR == int(n * p) + (int(n * p) < n ? 1 : 0) {printf "%.3f", $1; exit}' "$TMP/sorted"
}

printf 'requests measured  : %s\n' "$total"
printf 'added p50          : %s ms\n' "$(pct 0.50)"
printf 'added p90          : %s ms\n' "$(pct 0.90)"
printf 'added p99          : %s ms\n' "$(pct 0.99)"
printf 'added p99.9        : %s ms\n' "$(pct 0.999)"
printf 'added max          : %.3f ms\n' "$(tail -1 "$TMP/sorted")"
printf '\n'

pct_handshakes=$(awk -v h="$handshakes" -v n="$total" 'BEGIN {printf "%.1f", h * 100 / n}')
printf 'fresh TCP connects : %s (%s%% of requests)\n' "$handshakes" "$pct_handshakes"

over_budget=$(awk -v v="$(pct 0.50)" 'BEGIN {print (v > 1.0) ? 1 : 0}')
if [[ "$over_budget" == "1" ]]; then
    cat <<'HINT'

Added p50 is over one millisecond. Check, in this order:
  1. The fresh-connect percentage above. Anything more than a few percent
     means the upstream keepalive pool is undersized and most requests are
     paying for a TCP handshake.
  2. Regex locations in the generated config: matched linearly, in order.
  3. TLS on the internal hop, if you added it.
  4. access_log without buffer=, which writes to disk once per request.
HINT
fi

if awk -v p="$pct_handshakes" 'BEGIN {exit !(p > 5)}'; then
    cat <<'HINT'

More than five percent of requests opened a new connection to a backend.
Raise `keepalive` on that upstream in the control panel: every one of those
is a TCP handshake the gateway did not need to pay for.
HINT
fi
