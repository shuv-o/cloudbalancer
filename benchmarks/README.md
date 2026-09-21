# Benchmarking the gateway

The number worth measuring is the gateway's *added* latency: what routing,
cache lookup and proxying cost, with the backend's own work and the network
hop to it subtracted out. Two independent methods are set up here, and they
should agree.

## Method A — from Nginx's own log

    ./added_latency_from_logs.sh

Free, always on, and needs no second machine. The access log records
`rt=` (total request time) and `urt=` (upstream response time); the difference
is the proxy's contribution. It also reports how often a request had to open a
fresh TCP connection to the backend, which is the single most common reason
for the number being worse than it should be.

## Method B — A/B against a null backend

    docker compose -f docker-compose.yml -f docker-compose.bench.yml up -d
    RATE=5000 DURATION=60s ./compare.sh

Sends the same request at the same fixed rate directly to a backend and then
through the gateway, and prints the difference at each percentile.

Three things make the result trustworthy:

**Constant rate, not closed loop.** `vegeta` issues at a fixed rate regardless
of how fast responses come back. A closed-loop generator waits for each
response before sending the next, so when the system slows down the generator
slows down with it, and the latency it reports is the latency of a system that
was never actually under pressure.

**A backend that does nothing.** The mock returns a fixed string. If the
backend did real work, its variance would be larger than the entire quantity
being measured.

**A discarded warm-up.** The first seconds of any run are cold caches and
empty keepalive pools. `compare.sh` throws them away.

## Reading the result

Expect an added p50 in the tens of microseconds and a p99 in the low hundreds.
If it is worse, check in this order:

1. `uct=` in the access log. Non-zero on most requests means the keepalive pool
   is undersized and every request is paying for a TCP handshake.
2. Regex locations in the generated config. They are matched linearly, in
   source order, unlike prefix locations.
3. TLS on the internal hop. Terminate at the gateway and use plain HTTP to
   backends on a trusted LAN.
4. `access_log` without `buffer=`, which writes to disk once per request.

## Sweeping the rate

A single data point says little. Sweep it and watch for the knee:

    for r in 1000 5000 10000 20000 40000; do
        RATE=$r DURATION=30s ./compare.sh | tail -5
    done

A flat added-latency line across the sweep means there is headroom. A knee
shows where workers or file descriptors run out.
