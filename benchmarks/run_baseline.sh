#!/usr/bin/env bash
# Leg one: straight at a backend instance, with no gateway in the path.
set -euo pipefail
source "$(dirname "$0")/lib.sh"
require vegeta

echo "Baseline: direct to backend"
echo "  target ${BACKEND_URL}${BENCH_PATH}"
attack baseline "${BACKEND_URL}${BENCH_PATH}"
