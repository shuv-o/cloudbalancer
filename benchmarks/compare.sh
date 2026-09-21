#!/usr/bin/env bash
# =============================================================================
# Run both legs back to back and print what the gateway cost.
#
# Both legs use the same client, the same rate and the same duration, against a
# backend that does no work -- so the difference between them is the proxy and
# nothing else.
#
#   RATE=10000 DURATION=90s ./compare.sh
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require vegeta
require jq

"$BENCH_DIR/run_baseline.sh"
echo
"$BENCH_DIR/run_gateway.sh"
echo

read_stat() { jq -r ".latencies.$2" < "$RESULTS_DIR/$1.json"; }

printf '\n%s\n' "=============================================================="
printf '%s\n' " Gateway overhead at ${RATE} req/s over ${DURATION}"
printf '%s\n' "=============================================================="
printf '%-12s %12s %12s %12s\n' "percentile" "direct" "gateway" "added"

for p in mean p50 p90 p95 p99; do
    d=$(read_stat baseline "$p")
    g=$(read_stat gateway "$p")
    printf '%-12s %10s ms %10s ms %10s ms\n' \
        "$p" "$(ms "$d")" "$(ms "$g")" "$(ms "$((g - d))")"
done

direct_ok=$(jq -r '.success * 100' < "$RESULTS_DIR/baseline.json")
gateway_ok=$(jq -r '.success * 100' < "$RESULTS_DIR/gateway.json")
printf '\n%-12s %10.2f %%  %10.2f %%\n' "success" "$direct_ok" "$gateway_ok"

added_p50=$(( $(read_stat gateway p50) - $(read_stat baseline p50) ))
printf '\n'
if (( added_p50 < 1000000 )); then
    printf 'Added p50: %s ms — within the sub-millisecond budget.\n' "$(ms $added_p50)"
else
    printf 'Added p50: %s ms — over the budget. Check, in this order:\n' "$(ms $added_p50)"
    printf '  1. uct= in the access log. Non-zero on most requests means the\n'
    printf '     upstream keepalive pool is undersized and every request is\n'
    printf '     paying for a fresh TCP connection.\n'
    printf '  2. Regex locations in the generated config: they are matched\n'
    printf '     linearly, in order, unlike prefix locations.\n'
    printf '  3. TLS on the internal hop, if you added it.\n'
    printf '  4. access_log without buffer=, which writes once per request.\n'
fi

printf '\nRaw reports: %s\n' "$RESULTS_DIR"
