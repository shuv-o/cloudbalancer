#!/usr/bin/env bash
# Leg two: the same request, the same rate, through the gateway.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require vegeta

echo "Gateway: through the proxy"
echo "  target ${GATEWAY_URL}${BENCH_PATH}  (Host: ${GATEWAY_HOST})"
attack gateway "${GATEWAY_URL}${BENCH_PATH}" "$GATEWAY_HOST"
