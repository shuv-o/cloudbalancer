#!/usr/bin/env bash
# Shared helpers for the benchmark scripts.
set -euo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="${RESULTS_DIR:-$BENCH_DIR/results}"
mkdir -p "$RESULTS_DIR"

# Constant rate, not "as fast as possible". A closed-loop generator waits for
# each response before sending the next, so when the system slows down it also
# slows down -- and the latency it reports is the latency of a system that was
# never actually pushed. Open-loop at a fixed rate is the only way to see what
# a real arrival pattern would experience.
RATE="${RATE:-5000}"
DURATION="${DURATION:-60s}"
WARMUP="${WARMUP:-15s}"
CONNECTIONS="${CONNECTIONS:-128}"

BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:9101}"
GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1}"
GATEWAY_HOST="${GATEWAY_HOST:-demo.local}"
BENCH_PATH="${BENCH_PATH:-/bench}"

require() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "error: $1 is not installed." >&2
        case "$1" in
            vegeta) echo "  macOS: brew install vegeta" >&2
                    echo "  Linux: go install github.com/tsenart/vegeta/v12@latest" >&2 ;;
            jq)     echo "  macOS: brew install jq    Linux: apt install jq" >&2 ;;
        esac
        exit 1
    }
}

# Runs one leg: a discarded warm-up so keepalive pools and caches are hot, then
# the measured run.
attack() {
    local label="$1" url="$2" host_header="${3:-}"
    local -a header_args=()
    [[ -n "$host_header" ]] && header_args=(-header "Host: $host_header")

    echo "  warming up (${WARMUP}, discarded)..."
    echo "GET $url" | vegeta attack \
        -rate="$RATE" -duration="$WARMUP" -keepalive \
        -max-workers="$CONNECTIONS" "${header_args[@]}" >/dev/null

    echo "  measuring (${DURATION} at ${RATE}/s)..."
    echo "GET $url" | vegeta attack \
        -rate="$RATE" -duration="$DURATION" -keepalive \
        -max-workers="$CONNECTIONS" "${header_args[@]}" \
        > "$RESULTS_DIR/$label.bin"

    vegeta report -type=json < "$RESULTS_DIR/$label.bin" > "$RESULTS_DIR/$label.json"
    vegeta report < "$RESULTS_DIR/$label.bin" | tee "$RESULTS_DIR/$label.txt"
}

ms() { awk "BEGIN{printf \"%.4f\", $1/1000000}"; }
