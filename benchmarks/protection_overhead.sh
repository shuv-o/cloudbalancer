#!/usr/bin/env bash
# =============================================================================
# What the per-address protections cost on the hot path.
#
# The gateway's budget is one millisecond, and rate limiting, connection
# limiting and the blocklist all run per request. This measures them rather
# than assuming: the same load, through the same gateway, with the protections
# on and then off.
#
# Expect the difference to be single-digit microseconds. If it is not, the
# likely cause is lock contention on the shared limit_req zone at high
# concurrency, which shows up as a widening gap at p99 rather than at p50.
#
#   RATE=10000 DURATION=45s ./protection_overhead.sh
# =============================================================================
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require vegeta
require jq

DJANGO="${DJANGO_CONTAINER:-proxy-balancer-django}"

set_protection() {
    docker compose exec -T "$DJANGO" python manage.py shell -c "
from apps.security.models import TrafficProtectionPolicy
from apps.gateway.services import config_deploy
p = TrafficProtectionPolicy.load()
p.enabled = $1
p.save()
log = config_deploy()
print(log.status)
" >/dev/null
}

original=$(docker compose exec -T "$DJANGO" python manage.py shell -c \
    "from apps.security.models import TrafficProtectionPolicy
print(TrafficProtectionPolicy.load().enabled)" | tr -d '\r\n ')

restore() {
    echo
    echo "Restoring protection to its original setting (${original})..."
    set_protection "$original"
}
trap restore EXIT

echo "Leg 1: protections on"
set_protection True
sleep 2
attack protected "${GATEWAY_URL}${BENCH_PATH}" "$GATEWAY_HOST"

echo
echo "Leg 2: protections off"
set_protection False
sleep 2
attack unprotected "${GATEWAY_URL}${BENCH_PATH}" "$GATEWAY_HOST"

read_stat() { jq -r ".latencies.$2" < "$RESULTS_DIR/$1.json"; }

printf '\n%s\n' "=============================================================="
printf '%s\n' " Cost of per-address protection at ${RATE} req/s"
printf '%s\n' "=============================================================="
printf '%-12s %12s %12s %12s\n' "percentile" "off" "on" "added"

for p in p50 p90 p99; do
    off=$(read_stat unprotected "$p")
    on=$(read_stat protected "$p")
    printf '%-12s %10s ms %10s ms %10s ms\n' \
        "$p" "$(ms "$off")" "$(ms "$on")" "$(ms "$((on - off))")"
done

added_p99=$(( $(read_stat protected p99) - $(read_stat unprotected p99) ))
printf '\n'
if (( added_p99 > 200000 )); then
    printf 'The gap at p99 is %s ms, which is wider than a shared-memory lookup\n' "$(ms $added_p99)"
    printf 'should cost. That shape — fine at p50, wide at p99 — is lock contention\n'
    printf 'on the limit_req zone. Options, in order:\n'
    printf '  1. Raise the per-address rate so fewer requests reach the limiter.\n'
    printf '  2. Drop the global limit and keep per-route limits on expensive paths only.\n'
    printf '  3. Move rate limiting upstream, to something built to absorb it.\n'
else
    printf 'Added p99: %s ms. The protections are not the bottleneck.\n' "$(ms $added_p99)"
fi
