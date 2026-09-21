# Proxy Balancer

A self-hosted API gateway: one public address in front of many backend
services, with routing, load balancing, HTTP caching, automatic TLS, and a
control panel to run it all from.

The design goal that shapes everything else is that the gateway's own overhead
stays under a millisecond. That is why Django is nowhere near the request path.

---

## How it fits together

```
                       DNS  →  one public address
                                    │
                        ┌───────────▼────────────┐
                        │  Nginx     DATA PLANE  │   the only thing on the
                        │  TLS · routing         │   request path
                        │  cache · balancing     │
                        └───────────┬────────────┘
             plain HTTP/1.1, connections held open, same LAN
        ┌──────────────┬────────────┴───────┬──────────────┐
    backend-a      backend-b            backend-c      backend-d
    (3 instances)  (2 instances)        (1)            (4)

    ┌──────────────── CONTROL PLANE, off the request path ──────────────┐
    │  Django REST API  →  Jinja2  →  conf.d/*.conf                     │
    │                   →  nginx -t  →  nginx -s reload                 │
    │  Celery: deploys, health probes, certificate renewal              │
    │  Postgres (source of truth) · Redis · Prometheus · Grafana        │
    └───────────────────────────────────────────────────────────────────┘
```

The control plane never proxies anything. It writes three files and sends a
signal. Every millisecond a request costs is Nginx's, which is what makes the
latency budget achievable at all.

## Getting started

```bash
cp .env.example .env     # then edit it
make init                # build, start, create the admin account
```

For a real server — firewall, DNS, the first certificate, backups — and for
working out what is wrong when something misbehaves, see
**[DEPLOY.md](DEPLOY.md)**.

Then open **http://localhost:8081**.

The panel binds to loopback by default. It is an admin surface and should not
be one host header away from the internet; put it behind a VPN, or set
`PANEL_BIND` to a LAN address you control.

To see it working end to end without any real services:

```bash
make bench-up            # mock backends that return a fixed response
make seed                # a demo topology pointing at them
```

## What the panel does

| Page | What it is for |
|---|---|
| **Overview** | The request path end to end — arriving, routed, cached, upstream — with live counters on each stage |
| **Domains** | Hostnames the gateway answers for |
| **Routes** | Which requests reach which backend, with caching and rate limits per route |
| **Backends** | Services and their instances, how traffic is spread, health |
| **Certificates** | Issue, renew, upload; automatic renewal for anything from an authority |
| **Cache** | Hit ratio, what happened to each request, drop a single URL |
| **Deploys** | Every config change, what it rendered, and why one was rejected |
| **Security** | How the panel is reached, who has been reaching it, and the audit trail |

---

## Routing

Three axes, nested, cheapest first:

1. **Hostname → server block.** An exact hash lookup. This is "which tenant".
2. **Path → location → upstream.** A compiled prefix tree. This is "which
   service". Regex is available and flagged in the panel, because patterns are
   tested one at a time, in order, on every request.
3. **Header → a different upstream.** An escape hatch for canaries and
   per-tenant pools, rendered as an Nginx `map`.

Nginx matches a variable `proxy_pass` against declared upstream names before it
considers DNS, so header routing keeps the upstream connection pool; the cost
is one hash lookup per request.

A route that switches on a header and also caches gets that header folded into
its cache key automatically. Without that, the canary's response would be
stored under the same key as the stable one and served to everybody.

## Load balancing

A separate decision from routing, made inside each backend: round robin, least
connections, or client IP. Every upstream is rendered with a shared memory
`zone`, which is what makes least-connections accurate — without it each worker
counts only its own connections and makes a locally-optimal, globally-wrong
choice.

Connections to backends are held open and sized per backend. This matters more
than any other single setting: without it every proxied request pays for a TCP
handshake.

**Failover is Nginx's job**, per request and in real time. The health probes in
the panel exist to tell an operator what is happening. They only steer traffic
if you turn on draining, and then only after several consecutive results —
because each drain costs a reload, and a flapping backend would otherwise
produce a continuous stream of them.

## Caching

Off by default, on per route. Once on:

- **The backend's own headers win.** `Cache-Control`, `Expires` and `ETag` from
  upstream take precedence over the TTL in the panel, which is a fallback for
  responses that say nothing.
- **`Vary` is honoured**, never ignored. Ignoring it would cache one variant
  and serve it to every client regardless of what they asked for.
- **Expired entries revalidate** with `If-None-Match` / `If-Modified-Since`, so
  an unchanged object costs a 304 rather than a fresh transfer.
- **stale-while-revalidate**: the stale copy goes out immediately, one request
  refreshes it in the background, and the rest do not queue behind the backend.
- **Signed-in requests bypass and are never stored.** Both are needed: bypass
  alone would still write the response into the cache for the next anonymous
  caller. Turning this off is possible and takes a second, explicit
  confirmation, because it is how one user's data ends up served to another.
- **`X-Cache-Status` on every response**, including routes that do not cache,
  where it reads `-` rather than being absent.

`Accept-Encoding` is normalised before it enters the cache key. Clients send
dozens of spellings of the same three encodings, and keying on the raw header
shatters one cached object into one entry per client string.

**Purging a single URL** works without the commercial purge module. Nginx
stores each entry at a path derived from the MD5 of its cache key, split by the
`levels=1:2` setting, so the path is derivable and the file can simply be
removed. Emptying the whole cache is also available and is the blunter option —
every request misses until it refills.

## Automatic TLS

Certificates come from Let's Encrypt (or any ACME authority) over the HTTP-01
challenge, and renew on their own thirty days before expiry.

The usual bootstrap problem does not arise here. **Every domain is deployed
with a port-80 listener that already answers the challenge path**, from the
moment it is created and whether or not it has a certificate. So by the time
anyone asks for one, the challenge is already answerable — and because renewals
happen while the site is on HTTPS, that listener stays in place forever.

Self-signed certificates are available for internal hostnames no public
authority will validate, and PEM pairs can be uploaded directly. The gateway
checks that an uploaded key actually matches its certificate before storing
either — a mismatched pair stops Nginx from starting, which would take down
every domain, not just the one being changed.

Start with the staging authority. Production allows five failed attempts per
hostname per hour, and a typo costs one of them.

## Reaching the panel

On loopback by default, because reaching it is equivalent to controlling every
hostname the gateway serves. An SSH tunnel is the safest way in and costs one
command:

```bash
ssh -L 8081:localhost:8081 you@gateway
```

It can also be published on a hostname of its own, with controls that fail
independently of each other: an address allowlist enforced in both Nginx and
Django, optional client certificates, a mandatory authenticator app, per-address
rate limits, account lockout, idle and absolute session limits, a content
security policy that permits no third-party origin, and an append-only audit
trail. The Django admin and the metrics endpoint are not served publicly.

The loopback listener is never generated from that policy, so no setting on the
Security page can lock an operator out of the machine. See
[DEPLOY.md](DEPLOY.md) for the procedure.

## Deploys

```
render  →  back up  →  write  →  nginx -t  →  reload
```

A configuration that fails its check is restored from the backup before Nginx
ever sees it, so a mistake in the panel cannot take traffic down. Every deploy
is recorded with the exact config it rendered and, if it failed, why.

Writes are **coalesced**: editing five routing rules costs one reload a few
seconds later, not five. Each reload starts fresh workers whose connection
pools to the backends are empty, so they are worth batching.

Only three files are managed — `00-maps.conf`, `10-upstreams.conf`,
`20-servers.conf`. Anything else you drop into `conf.d` is left alone.

---

## Performance

### Where the budget goes

Expect Nginx to add tens of microseconds at p50. The things that actually spend
the budget, in the order worth checking:

1. **Connections to backends not being reused.** `uct=` in the access log is
   non-zero when a request had to open a new TCP connection. More than a few
   percent means the pool is undersized.
2. **Regex locations on hot paths**, matched one at a time in order.
3. **TLS on the internal hop.** Terminate at the gateway; use plain HTTP to
   backends on a trusted LAN.
4. **Unbuffered access logs**, one disk write per request. Every generated log
   here uses `buffer=64k flush=5s`.

### Measuring it

Two independent methods, which should agree:

```bash
make bench-log     # from Nginx's own counters, always available
make bench         # A/B against a backend that does no work
```

The first is the one to trust. Nginx records total request time and upstream
response time separately, so their difference is the proxy's own contribution —
per request, on real traffic, with no load generator involved. The same
subtraction is charted on the Overview page and in Grafana.

The second sends the same request at the same fixed rate directly to a backend
and then through the gateway. It uses a constant-rate generator rather than a
closed loop: a generator that waits for each response before sending the next
slows down when the system does, and reports the latency of a system that was
never actually under pressure.

See [`benchmarks/README.md`](benchmarks/README.md) for the full method.

### Why Nginx

| | |
|---|---|
| **HAProxy** | The best L7 router and health checker there is, with a runtime API that changes servers without a reload. But no meaningful HTTP cache — you would run Varnish behind it, and maintain two config languages and two failure modes. |
| **Traefik** | Excellent dynamic configuration, which would replace this whole control plane. But more p99 variance under load, slower router matching, and caching is a paid feature. |
| **Angie** | An Nginx fork that ships active health checks, Prometheus metrics, and a no-reload upstream API for free. Drop-in compatible with this configuration — the natural upgrade path if reload churn ever starts to hurt. |

Nginx is the only one of the four that does routing *and* a real disk cache
with stale-while-revalidate in a single process.

## Metrics

Prometheus scrapes Nginx directly. The traffic-status module compiled into the
gateway image serves its own Prometheus endpoint, so there is no exporter to
run or to fall behind.

That module is not optional. `stub_status` — what the stock image offers —
reports connection counts and a request total and nothing else: no cache
statuses, no per-vhost breakdown, no upstream timing. A cache hit ratio, the
number this gateway exists to show, cannot be derived from it.

Grafana at **http://localhost:3000** has a provisioned dashboard covering added
latency, throughput, cache behaviour and per-instance load.

---

## Flooding and abuse

Per-address limits apply to every public domain: 50 requests per second, 64
concurrent connections, short header and body timeouts, and SYN cookies in the
gateway container. There is a blocklist that answers with `444`, and routes can
be told to cache by path alone so a query-string flood cannot bust the cache.

All of that bounds what **one address** can consume. None of it stops a
volumetric flood — once the uplink is saturated the packets never reach this
machine, and nothing configured here is involved. A large distributed flood
staying under the per-address limit is equally out of reach, because from here
it is indistinguishable from a lot of ordinary clients. Both need a scrubbing
service in front of the gateway. See [DEPLOY.md](DEPLOY.md).

The protections cost single-digit microseconds per request: `limit_conn` is per
connection and amortised by keepalive, `limit_req` is one shared-memory lookup,
and the blocklist emits nothing at all when it is empty. Every rate limit uses
`nodelay`, so excess requests are refused rather than delayed — a limiter that
queues would convert itself into the latency it was meant to prevent. Measure
it with `make bench-protection`.

## Known trade-offs

**One gateway is a single point of failure.** One A record, one machine. The HA
path is keepalived with a shared address and a second gateway — at the cost of
roughly halving the cache hit ratio, since each node caches on its own.

**Two sources of health truth.** Nginx's real-time passive checks and the
panel's probes will sometimes disagree. Passive is authoritative for traffic;
the probes are for the dashboard and for hysteresis-gated draining.

**Reloads are not free.** Graceful, but fresh workers start with empty
connection pools. Hence the coalescing, and hence the reluctance to let a
flapping health probe rewrite the config.

**Caching is the sharpest edge in the product.** Everything else fails loudly.
A wrong cache rule fails quietly, by serving the right response to the wrong
person. That is why it is off by default, per route, and why turning off the
authenticated bypass takes a second confirmation.

## Layout

```
gateway/nginx/       Dockerfile (Nginx + traffic-status), nginx.conf, conf.d/
management/          Django control plane
  apps/domains/        hostnames
  apps/backends/       services, instances, health
  apps/routing/        rules, header overrides, deploy log
  apps/certificates/   ACME, self-signed, import, renewal
  apps/security/       panel exposure, second factors, lockout, audit
  apps/gateway/        config rendering, deploy, cache purge
  apps/monitoring/     traffic status and Prometheus queries
frontend/            control panel (React, no runtime server)
observability/       Prometheus and Grafana provisioning
benchmarks/          mock backends and the latency harness
```

Django follows the Services + Selectors pattern: `services.py` changes state,
`selectors.py` reads, `apis.py` stays thin.
