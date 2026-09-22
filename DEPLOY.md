# Deploying and diagnosing

Two halves: getting this onto a real server, and finding out what is wrong when
something misbehaves. Every command here is meant to be pasted.

---

# Part 1 — Deploy

## Before you start

On the gateway machine:

- Docker Engine 24+ and the Compose plugin
- 2 GB RAM minimum, 4 GB if the cache is going to be busy
- Disk for the cache — the default is a 2 GB cap in `nginx.conf`
- The backends reachable on the LAN, on the addresses you will enter in the panel

Off the machine:

- A **DNS A record** for each hostname, pointing at this server's public address
- **Ports 80 and 443 reachable from the internet.** Port 80 is not optional even
  if you only serve HTTPS: it is where certificate authorities validate, on
  first issue and on every renewal.

Check DNS resolves before you go further, because certificate issuance will fail
on it and cost you a rate-limited attempt:

```bash
dig +short api.example.com     # must return this server's public address
```

## First deploy

```bash
git clone <your-repo> /opt/cloudbalancer
cd /opt/cloudbalancer
cp .env.example .env
```

Edit `.env`. The five that matter before anything is exposed:

```ini
POSTGRES_PASSWORD=<long random string>
DJANGO_SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(50))">
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=api.example.com,portal.example.com
ADMIN_PASSWORD=<your operator password>
```

Leave `ACME_STAGING=true` for now. Then:

```bash
make init
```

That builds the images (including Nginx with the traffic-status module
compiled in — the first build takes a few minutes), starts everything, applies
migrations, and creates the admin account.

Confirm it came up:

```bash
make ps
curl -s localhost/health          # {"status":"ok"} from the catch-all server
```

## Reaching the control panel

The panel binds to `127.0.0.1:8081` on purpose. It is an admin surface and does
not belong on a public interface. From your workstation:

```bash
ssh -L 8081:localhost:8081 you@gateway.example.com
```

Then open **http://localhost:8081** locally.

To serve it on a public hostname instead, see **[Part 3](#part-3--publishing-the-control-panel)** —
it is supported, and it is more involved than changing a bind address.

> **Docker publishes past ufw.** A published port writes its own iptables rules
> that `ufw deny` does not cover. Binding to a specific address in the compose
> file is the control that actually works here; a host firewall alone is not.

## Your first certificate

1. In the panel, **Domains → Add domain**. The gateway starts accepting the
   hostname immediately and begins answering certificate challenges for it.
2. **Certificates → Connect an authority.** Enter a contact address, accept the
   subscriber agreement. Leave it on staging for the first run.
3. **Get a certificate.** Pick the hostname, request.
4. Watch it happen:

   ```bash
   make logs-celery
   ```

5. A staging certificate appears, marked *not browser-trusted*. That is the
   point — it proves DNS, port 80 and the challenge path all work, without
   spending one of your five production attempts per hostname per hour.

**Switching to production:** set `ACME_STAGING=false` in `.env`, then add a
production authority in **Certificates → Connect another** and request again.
The staging certificate is replaced.

Renewal then runs on its own, daily at 03:17, for anything within 30 days of
expiry. Nothing to schedule and nothing to remember.

## Production checklist

```bash
grep -E 'DEBUG|SECRET_KEY|ALLOWED_HOSTS|PASSWORD|BIND' .env
```

- [ ] `DJANGO_DEBUG=False`
- [ ] `DJANGO_SECRET_KEY` is not the example value
- [ ] `DJANGO_ALLOWED_HOSTS` lists your real hostnames, not `*`
- [ ] `POSTGRES_PASSWORD` and `GF_SECURITY_ADMIN_PASSWORD` changed
- [ ] `PANEL_BIND` and `OBSERVABILITY_BIND` still `127.0.0.1`, or a private address
- [ ] `ACME_STAGING=false` once issuance is proven
- [ ] Ports 80 and 443 open; 8081, 3000 and 9090 not
- [ ] `make check` passes

Everything restarts with `unless-stopped`, so the stack comes back after a
reboot as long as the Docker daemon is enabled:

```bash
sudo systemctl enable docker
```

## Backups

Two volumes hold state you cannot regenerate:

```bash
# The source of truth for every domain, backend and route.
docker compose exec -T postgres pg_dump -U cloudbalancer cloudbalancer \
  | gzip > backup-$(date +%F).sql.gz

# Certificates and ACME account keys.
docker run --rm -v cloudbalancer_letsencrypt:/data -v "$PWD":/out alpine \
  tar czf /out/letsencrypt-$(date +%F).tar.gz -C /data .
```

`nginx_cache` needs no backup — it refills itself. `prometheus_data` is history
only; losing it costs you charts, not configuration.

To restore, load the dump and run `make deploy`: the config is rendered from the
database, so nothing else needs restoring.

## Upgrading

```bash
git pull
make build
docker compose up -d
make migrate
make deploy
```

Nginx reloads gracefully, so in-flight requests finish on the old workers.

### Coming from an install that predates the CloudBalancer name

Only relevant if you deployed while this was called `proxy_balancer`. The
rename changed the Compose project name, which prefixes every volume, and the
Postgres database and role. A plain `git pull` will therefore come up with
empty volumes and an empty database rather than an error, which is the
confusing kind of failure.

Take a dump under the old name first:

```bash
docker compose -p proxy_balancer exec -T postgres \
  pg_dump -U proxy_balancer proxy_balancer | gzip > pre-rename.sql.gz

docker run --rm -v proxy_balancer_letsencrypt:/data -v "$PWD":/out alpine \
  tar czf /out/pre-rename-letsencrypt.tar.gz -C /data .
```

Then bring up the renamed stack and load it back:

```bash
make build && docker compose up -d postgres
gunzip -c pre-rename.sql.gz \
  | sed 's/proxy_balancer/cloudbalancer/g' \
  | docker compose exec -T postgres psql -U cloudbalancer cloudbalancer

docker run --rm -v cloudbalancer_letsencrypt:/data -v "$PWD":/in alpine \
  tar xzf /in/pre-rename-letsencrypt.tar.gz -C /data

docker compose up -d
make deploy
```

The `sed` covers the role name embedded in the dump's ownership statements.
Nothing else in the data carries the old name — configuration is rendered from
the database, so the generated Nginx files regenerate on the first deploy.

The old volumes are left untouched, so this is reversible until you remove
them yourself.

---

# Part 2 — Diagnose

## Start here, always

```bash
make ps       # is everything actually running
make check    # does the live configuration pass nginx -t
make logs     # what is each service saying
```

Then the panel's **Deploys** page, which records every config change, what it
rendered, and the exact error if one was rejected.

## Symptom → cause

### A domain returns `{"error":"No domain is configured for this host."}`

The request reached the gateway and matched no server block. Either the domain
is not deployed, or the Host header is not what you think.

```bash
make config | grep -A 3 "server_name"          # what would be rendered
grep server_name gateway/nginx/conf.d/20-servers.conf   # what is live
```

If the domain is in the first and not the second, a deploy has not run or has
failed. `make deploy` and read the output.

### A domain returns `{"error":"No route is configured for this path"}`

The hostname matched; no rule inside it did. Check the **Routes** page: rules
are evaluated from the lowest priority number upward, and the first match wins.
A catch-all at priority 100 with nothing below it means anything not matching a
lower-numbered rule lands here.

A rule also silently does not deploy if its backend has no active instances —
an upstream with no servers fails `nginx -t`, so the renderer drops the rule
rather than let one bad route break every domain. That drop is logged:

```bash
make logs-celery | grep "Skipping rule"
```

### 502 or 504

The route matched and the backend did not answer.

```bash
docker compose exec nginx tail -50 /var/log/nginx/api.example.com.error.log
```

Then check the instance is reachable *from the gateway*, which is not the same
as reachable from your laptop:

```bash
docker compose exec nginx wget -qO- http://10.0.1.11:8080/health
```

The **Backends** page shows the same thing from the prober's side. A backend
showing "2 of 3 up" with 502s in the log usually means the third instance is
failing and Nginx is retrying past it — which is working as intended, just
noisily.

### A deploy failed

Nothing broke. A configuration that fails `nginx -t` is restored from backup
before Nginx sees it, so the gateway kept serving whatever it had.

Read the error on the **Deploys** page — it is `nginx -t` output, which names
the file and line. The rendered config is stored with the failed deploy, so you
can see exactly what produced it.

Backups of the managed files are on the host:

```bash
ls gateway/nginx/conf.d/.backups/
```

### A certificate will not issue

The failure line is on the **Certificates** page, and the full certbot output
is in the worker log:

```bash
make logs-celery | grep -i -A 20 certbot
```

The three common causes, in order:

**DNS does not point here.** `dig +short api.example.com` must return this
server's public address. The error says `NXDOMAIN` or names a different address.

**Port 80 is not reachable from outside.** The authority validates over plain
HTTP regardless of whether you serve HTTPS. Test the challenge path end to end:

```bash
# Written from the management container: nginx mounts this directory read-only.
docker compose exec django sh -c \
  'mkdir -p /var/www/acme/.well-known/acme-challenge && echo test > /var/www/acme/.well-known/acme-challenge/probe'

curl http://api.example.com/.well-known/acme-challenge/probe    # must print: test
docker compose exec django rm /var/www/acme/.well-known/acme-challenge/probe
```

If that returns 404 from outside but works locally, it is a firewall or an
upstream NAT, not the gateway.

**Rate limited.** Production allows five failed attempts per hostname per hour.
The error says so explicitly. Switch to staging, fix the real problem there,
then come back.

### A WebSocket connects and then drops

Almost always the idle timeout. Turn on **Stream the response** for the route
and set **Close after idle** longer than your application's heartbeat — the
panel defaults it to an hour when you enable streaming, because the
request-response default of 60 seconds silently closes any socket that goes
quiet for a minute.

Check what is actually deployed:

```bash
grep -A 4 'location /socket/' gateway/nginx/conf.d/20-servers.conf
```

A working streaming route has all of these:

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade    $http_upgrade;
proxy_set_header Connection $connection_upgrade;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
proxy_buffering off;
```

If the handshake itself fails rather than dropping later, it is one of:

**The route is not marked streaming.** Without it, `Connection` is cleared
rather than carrying the upgrade token, and the backend never sees an upgrade
request. The response will be a plain 200 instead of a 101.

**`$connection_upgrade` is undefined.** It comes from a `map` in
`00-maps.conf`; if that file is missing the variable is empty and the header is
dropped. `make config` will show it.

**Per-address connection limits.** WebSocket connections are long-lived, so
they occupy a slot for their whole lifetime. The default cap is 64 concurrent
connections per address, which a NAT'd office can reach with far fewer than 64
users. Raise it on Security → Traffic limits.

Confirm end to end:

```bash
curl -i -N -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
     -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
     https://ws.example.com/socket/
```

`HTTP/1.1 101 Switching Protocols` means the gateway is doing its part.

### The cache is not hitting

Ask a response directly:

```bash
curl -sI https://api.example.com/v1/things | grep -i x-cache-status
```

| Value | Meaning |
|---|---|
| `HIT` | Served from disk. Working. |
| `MISS` | Not cached yet. Ask twice — the first request always misses. |
| `BYPASS` | The cache was told to skip this. Usually an `Authorization` header or a session cookie. |
| `EXPIRED` | Was cached, aged out, refetched. |
| `STALE` / `UPDATING` | Served an old copy while refreshing. Working as designed. |
| `-` | This route does not cache at all. |

Persistent `MISS` on a route that should cache, in order of likelihood:

1. **The backend is saying no.** Upstream `Cache-Control: no-cache`, `no-store`
   or `private` wins over the TTL in the panel — that is deliberate. Check with
   `curl -sI` straight at the backend. Override it per route with *Cache even
   when the backend says not to*, if you are sure.
2. **`Set-Cookie` in the response.** Nginx will not cache a response that sets a
   cookie. Fix it at the backend.
3. **`cache_min_uses` above 1.** The response is not stored until it has been
   asked for that many times.
4. **The request is authenticated.** That is `BYPASS`, not `MISS`, and it is the
   safe default.

The **Cache** page breaks every request down by outcome, which is faster than
guessing.

### Added latency is over budget

Measure before you tune:

```bash
make bench-log
```

That reads `rt=` minus `urt=` out of the access log — the gateway's own cost,
on real traffic, with the backend subtracted. It also reports how often a
request had to open a fresh TCP connection.

Then work the list in order:

1. **`uct=` non-zero on more than a few percent of requests.** The keepalive
   pool is undersized and most requests are paying for a TCP handshake. Raise
   *Idle connections per worker* on that backend. This is almost always the
   answer.
2. **Regex locations.** Patterns are tested one at a time, in priority order, on
   every request. The Routes page flags them. Convert to a prefix if you can.
3. **TLS on the internal hop**, if you added it. Terminate at the gateway.
4. **Logging.** Every generated block uses `buffer=64k flush=5s`; a hand-written
   one in `conf.d` might not.

For an A/B against a backend that does no work:

```bash
make bench-up
RATE=5000 DURATION=60s make bench
```

### The panel will not load or will not sign in

```bash
docker compose logs django | tail -40
curl -s localhost:8081/api/v1/auth/me/     # from the gateway host
```

A blank page with a CSRF error in the browser console usually means
`CSRF_TRUSTED_ORIGINS` does not include the origin you are opening. If you are
tunnelling to `localhost:8081`, that origin is already in `.env.example`; if you
moved the panel to a LAN address, add it.

Forgotten password:

```bash
docker compose exec django python manage.py changepassword admin
```

### No charts, or "traffic counters unavailable"

The panel reads live counters straight from Nginx. Test the chain:

```bash
docker compose exec django curl -s http://nginx:8080/status/format/json | head -c 200
```

Empty or refused means the traffic-status module did not load:

```bash
# Loaded dynamically, so it does not appear in `nginx -V` configure arguments.
docker compose exec nginx ls -l /etc/nginx/modules/
docker compose exec nginx nginx -T 2>/dev/null | grep vhost_traffic_status_zone
docker compose logs nginx | grep -i emerg
```

If the module is genuinely missing, the image was built without it — rebuild
with `make build`. Charts specifically (as opposed to the live numbers) come
from Prometheus, so check it is scraping:

```bash
curl -s localhost:9090/api/v1/targets | grep -o '"health":"[a-z]*"'
```

## Reading the access log

Every line carries the fields that answer most questions without another tool:

```
rid=a1b2… cache=HIT rt=0.00042 uct=0.000 uht=0.00031 urt=0.00031 upstream=10.0.1.11:8080
```

| Field | What it tells you |
|---|---|
| `rid` | Request ID, also returned to the client as `X-Request-ID`. Give it to a user, find their exact request. |
| `cache` | Which cache outcome this request took. |
| `rt` | Total time, client to client. |
| `urt` | Time the backend took. **`rt` − `urt` is the gateway's own cost.** |
| `uct` | Time to open a connection. Should be `0.000` — anything else is a TCP handshake the pool should have avoided. |
| `uht` | Time to first response header from the backend. |
| `upstream` | Which instance answered. Two addresses means it retried. |

Useful one-liners:

```bash
# Follow one domain
docker compose exec nginx tail -f /var/log/nginx/api.example.com.access.log

# Every request that took over a second
docker compose exec nginx awk '$0 ~ /rt=/ {for(i=1;i<=NF;i++) if($i~/^rt=/ && substr($i,4)+0>1) print}' \
  /var/log/nginx/access.log

# Cache outcome distribution
docker compose exec nginx grep -o 'cache=[A-Z-]*' /var/log/nginx/access.log | sort | uniq -c | sort -rn

# Trace one request by its ID
docker compose exec nginx grep 'rid=a1b2c3' /var/log/nginx/access.log
```

## Where everything is

| What | Where |
|---|---|
| Generated config | `gateway/nginx/conf.d/{00-maps,10-upstreams,20-servers}.conf` — bind-mounted, readable on the host |
| Config backups | `gateway/nginx/conf.d/.backups/<timestamp>/` |
| Hand-written config | `gateway/nginx/conf.d/default.conf` — never overwritten by a deploy |
| Full merged config | `docker compose exec nginx nginx -T` |
| Gateway logs | `/var/log/nginx/` in the `nginx` container, volume `nginx_logs` |
| Certificates | `/etc/letsencrypt/live/<domain>/`, volume `letsencrypt` |
| Cache | `/var/cache/nginx/`, volume `nginx_cache` |
| ACME challenges | `/var/www/acme/`, volume `acme_challenge` |
| Deploys and errors | `celery` container log, and the panel's Deploys page |
| Certificate issuance | `celery` container log |

## Running things by hand

```bash
# Render the config without writing it
make config

# Deploy synchronously and see the result
docker compose exec django python manage.py deploy_config

# Probe every backend now
docker compose exec celery celery -A config call gateway.health_check_all

# Renew anything inside its renewal window
make renew

# Re-read certificate files and refresh expiry dates
docker compose exec celery celery -A config call certificates.sync_metadata
```

## When it is really broken

**Restore the last good configuration** without the control plane involved:

```bash
ls gateway/nginx/conf.d/.backups/
cp gateway/nginx/conf.d/.backups/20260921_143022_*/[0-9]*.conf gateway/nginx/conf.d/
docker compose exec nginx nginx -t && docker compose exec nginx nginx -s reload
```

**Take a domain out of service** without deleting anything: turn it off on the
Domains page, or if the panel is unreachable:

```bash
docker compose exec django python manage.py shell -c \
  "from apps.domains.models import Domain; Domain.objects.filter(name='api.example.com').update(is_active=False)"
make deploy
```

**Serve nothing but the catch-all**, the last resort:

```bash
mv gateway/nginx/conf.d/20-servers.conf /tmp/
docker compose exec nginx nginx -s reload
```

The control plane can be stopped entirely and the gateway keeps serving — it
holds no state Nginx needs at request time:

```bash
docker compose stop django celery celery-beat
```

That is the property worth remembering when something is on fire: the thing
that routes traffic and the thing that configures it are genuinely separate,
and only one of them has to be working.

---

# Part 3 — Publishing the control panel

The panel is on loopback by default because reaching it is equivalent to
controlling every hostname the gateway serves. Putting it on a public name is a
real increase in exposure, and the honest summary is this: **an SSH tunnel
remains the safest option, and costs one command.** If you publish anyway —
because a team needs it, or because tunnels do not survive contact with
on-call — build it in layers so that defeating one control still leaves the
others.

## The layers, in the order an attacker meets them

| Layer | What it does | Cost |
|---|---|---|
| **Address allowlist** | Nginx refuses the connection. Django checks the same list again. | Breaks access from anywhere unplanned |
| **Client certificate** | Refused during the TLS handshake — the sign-in form is never reached | Every operator needs a certificate installed |
| **Password** | 12 characters minimum, validated against common lists | — |
| **Authenticator app** | Required by default; enforced on every request, not just sign-in | Enrollment, and a recovery path |
| **Rate limit** | 10 sign-ins per minute per address, burst of 3, at the gateway | — |
| **Account lockout** | 5 failures locks for 15 minutes, counted per account | A locked-out operator waits |
| **Session limits** | 60 minutes idle, 12 hours absolute | Signing in again |
| **Audit trail** | Every change, with actor, address and time. Append-only. | — |

The two that matter most are the first two, because they stop an attacker before
any code that could have a bug in it runs.

## Publishing, step by step

**1. Add the hostname as a domain and get a certificate for it.**

In the panel: Domains → Add `control.shuvoo.com`, then Certificates → Get a
certificate. The panel refuses to publish without one — over plain HTTP its
session cookie is visible to anyone on the path, and that cookie is full control
of this gateway.

**2. Decide who can connect.**

Security → Publish the panel. Fill in the allowlist with the addresses your
operators actually come from:

```
203.0.113.0/24
198.51.100.17
```

Leaving it empty works and is the weakest setting available. The panel says so
on the page rather than letting it pass quietly.

**3. Consider client certificates.**

This is the strongest control here. Someone without a certificate signed by your
CA is refused during the handshake — no sign-in form, no password guessing, no
exposure of anything that parses input.

```bash
make panel-ca                 # once
make panel-cert NAME=alice    # per operator, produces alice.p12
```

Import the `.p12` into the operator's browser, then set the CA path in the
panel to `/etc/nginx/client-ca/panel-ca.pem` and turn on **Require a client
certificate**.

Back up `gateway/nginx/client-ca/panel-ca.key` somewhere you would keep a root
password, and nowhere else. Anyone holding it can issue themselves access.

The real cost is revocation: this verifies against the CA rather than a
revocation list, so removing one operator means reissuing everyone's
certificates. For a team where that is too coarse, keep the address allowlist as
the second control and disable the account instead.

**4. Enrol second factors.**

On by default. Every operator meets a mandatory enrollment screen before they
can use the panel — including accounts that already existed, because the
requirement is checked on every request rather than only at sign-in.

Recovery codes are shown once and stored hashed. If someone loses both their
device and their codes, another superuser clears it from Security → Operators,
and the reset is recorded in the audit trail.

**5. Deploy and check.**

Saving the policy triggers a deploy. Then verify from outside:

```bash
# Redirects to HTTPS
curl -sI http://control.shuvoo.com | head -1

# Security headers present
curl -sI https://control.shuvoo.com | grep -iE 'strict-transport|content-security|x-frame'

# The Django admin is not exposed
curl -so /dev/null -w '%{http_code}\n' https://control.shuvoo.com/admin/     # 404

# Metrics are not exposed
curl -so /dev/null -w '%{http_code}\n' https://control.shuvoo.com/metrics    # 404

# From an address outside the allowlist
curl -so /dev/null -w '%{http_code}\n' https://control.shuvoo.com/           # 403
```

And from the machine itself:

```bash
make panel-status
```

## If you lock yourself out

The loopback listener on port 8081 is deliberately outside all of this. It is
never generated from the policy, so no setting on the Security page can remove
it:

```bash
ssh -L 8081:localhost:8081 you@gateway
```

That still works with a broken allowlist, a wrong CA, an expired certificate, or
a policy that refuses every address. From there, undo whatever caused it.

If even the panel is unusable:

```bash
# Take the panel back off its public name
docker compose exec django python manage.py shell -c \
  "from apps.security.models import PanelAccessPolicy; \
   p = PanelAccessPolicy.load(); p.is_published = False; p.save()"
make deploy

# Clear a second factor for an operator who lost their device
docker compose exec django python manage.py shell -c \
  "from apps.security.models import TotpDevice; \
   TotpDevice.objects.filter(user__username='alice').delete()"

# Release a locked account early
docker compose exec django python manage.py shell -c \
  "from apps.security.models import AccountLock; \
   AccountLock.objects.filter(username='alice').delete()"
```

## What this does not do

Worth stating plainly, so nobody assumes otherwise:

- **No certificate revocation list.** Removing one operator's client
  certificate means reissuing the CA.
- **No SSO.** Accounts are local to this gateway.
- **The audit trail is append-only from the application**, but anyone with
  database access can still edit it. Ship it elsewhere if that matters.
- **Rate limits are per gateway**, not shared across a pair of them. Two
  gateways behind one address each allow the configured rate.
- **`unsafe-inline` remains in the style policy**, because the panel sets some
  layout values as style attributes. It is not in the script policy, which is
  where it would actually matter.

## Watching it

The Security page answers "is anyone trying?" — failed attempts in the last 24
hours, distinct addresses, locked accounts. Sustained failures from many
addresses is the shape of a real attempt rather than a forgotten password.

```bash
# Failed sign-ins, from the gateway
docker compose exec nginx grep -c ' 401 ' /var/log/nginx/panel.access.log

# Rate-limited requests
docker compose exec nginx grep -c ' 429 ' /var/log/nginx/panel.access.log

# Refused by the allowlist
docker compose exec nginx grep -c ' 403 ' /var/log/nginx/panel.access.log
```

---

# Part 4 — Flooding and abuse

## The short answer

This gateway bounds what **one address** can consume. It does not stop a
volumetric flood, and nothing running on a single machine can.

The distinction is where the traffic dies:

| Attack | Where it is stopped | By what |
|---|---|---|
| One host hammering an endpoint | Here | Per-address rate and connection limits |
| Slowloris, slow POST | Here | Short header and body timeouts |
| Credential stuffing on the panel | Here | Sign-in rate limit, account lockout, second factor |
| Scrapers, scanners, known-bad addresses | Here | Blocklist, refused with 444 |
| Cache-busting query floods | Here | Cache key that ignores the query string |
| SYN flood | Here, by the kernel | SYN cookies, a larger backlog |
| **10 Gbit/s of UDP at your address** | **Not here** | Your provider, or a scrubbing service |
| **100,000 hosts at 1 req/s each** | **Not here** | Upstream WAF, or something that can challenge clients |

The last two matter most and are worth being blunt about. A volumetric flood
saturates the uplink before a single packet reaches Nginx — there is no
configuration on this box that participates in the outcome. And a large
distributed flood staying under the per-address limit looks exactly like a lot
of ordinary clients, because from here it is indistinguishable from one.

**If you need to survive either, you need something in front of this gateway**
that has more capacity than the attack: Cloudflare, a provider's scrubbing
service, or an ISP willing to nullroute upstream. Buy that before tuning
anything below.

## What is on by default

Per-address limits apply to every public domain, underneath any tighter limit
a route sets for itself:

- **50 requests per second**, burst 100
- **64 concurrent connections**
- **10-second header and body timeouts**
- **SYN cookies** and an 8192-entry backlog in the Nginx container

Change them on Security → Traffic limits. A domain whose routes never set a
limit of their own is still covered, which is the point of having a floor.

## What it costs per request

Since the gateway's budget is one millisecond, anything on the request path has
to earn its place:

| Directive | When it runs | Cost |
|---|---|---|
| `limit_conn` | Per **connection**, so keepalive amortises it | Shared-memory lookup |
| `limit_req` | Per request | One shared-memory lookup under a per-zone lock |
| `geo` + `if` for the blocklist | Per request, **only when something is blocked** | Radix-tree lookup |

Single-digit microseconds against a 1000 µs budget. Two design choices keep it
that way, and both are asserted by tests:

**An unused control emits nothing.** With an empty blocklist there is no `geo`
table and no `if` in the generated config at all — not a check that passes
quickly, no check.

**Rate limits reject rather than delay.** Every `limit_req` carries `nodelay`.
Without it Nginx holds excess requests back to smooth them to the configured
rate, which converts a rate limit into added latency for exactly the traffic it
was supposed to let through.

Measure it rather than trusting the table:

```bash
make bench-protection
```

It runs the same load through the gateway with the protections on and then off,
and prints the difference at each percentile. A gap that is small at p50 and
wide at p99 is lock contention on the shared `limit_req` zone — at which point
raise the per-address rate, or drop the global limit and keep per-route limits
on the expensive paths only.

## Cache-busting

Worth its own note, because it turns your best defence into your worst
liability. A flood of `GET /?x=<random>` misses the cache on every request, so
the cache passes the entire load to the backend *and* writes a file per request
on the way through.

On routes where the query string does not change the response — static assets,
mostly — turn on **Cache by path alone** in the route's caching settings. The
key becomes `$uri` instead of `$request_uri`, and a million distinct query
strings collapse to one cached object.

Do not turn it on for a search endpoint. `?q=cats` and `?q=dogs` would be
served the same answer.

## Blocking addresses

Security → Refused addresses, or through the API so that fail2ban or CrowdSec
can write into it:

```bash
curl -X POST https://control.shuvoo.com/api/v1/security/blocked/ \
  -H "Content-Type: application/json" -H "X-CSRFToken: $TOKEN" -b "$COOKIES" \
  -d '{"cidr":"203.0.113.0/24","reason":"abuse","minutes":1440,"note":"scraper"}'
```

Blocked addresses get a `444` — the connection closes with no response sent.
The client learns nothing and the gateway spends almost nothing.

Two guardrails: a block covering the panel's own allowlist is refused, and
blocks expire by default. The commonest way a blocklist is misused is locking
yourself out of the thing you were defending.

**Automatic banning is deliberately not built in.** fail2ban and CrowdSec
already do this properly, with log parsing, decay and shared intelligence; a
log-tailing loop in Python would be a worse version of both. Point one of them
at `/var/log/nginx/*.access.log` and have it POST to the endpoint above.

## Is it happening?

```bash
# Rate-limited requests
docker compose exec nginx grep -c ' 429 ' /var/log/nginx/access.log

# Refused outright
docker compose exec nginx grep -c ' 444 ' /var/log/nginx/access.log

# Busiest addresses right now
docker compose exec nginx awk '{print $1}' /var/log/nginx/access.log \
  | sort | uniq -c | sort -rn | head -20

# Cache outcomes — a spike in MISS with steady traffic suggests cache-busting
docker compose exec nginx grep -o 'cache=[A-Z-]*' /var/log/nginx/access.log \
  | sort | uniq -c | sort -rn
```

The Overview page shows the same shape: requests climbing while the cache hit
ratio falls is the signature of a cache-busting flood, and it is visible there
before it is visible in the backends.

## If you are being flooded right now

In order:

1. **Confirm it is layer 7 and not volumetric.** If `make bench-log` still
   works and the gateway is responsive, the packets are arriving and Nginx is
   coping — that is an application-layer problem you can act on. If the machine
   is unreachable, stop reading and call your provider.

2. **Find the addresses**, with the `awk` one-liner above.

3. **Block the worst ranges**, with an expiry so the block heals itself.

4. **Tighten the per-address rate** on Security → Traffic limits. It deploys in
   a few seconds and costs one reload.

5. **Turn on cache-by-path** for routes being cache-busted, and raise
   `cache_min_uses` so one-off URLs are never stored.

6. **Let stale content serve.** Already on: `proxy_cache_use_stale` keeps the
   cache answering while the backends are struggling, which is exactly when it
   matters most.

7. **If the sources are too many and too distributed to block**, you have
   reached the limit of what a single gateway can do. Put a scrubbing service
   in front of it.

---

# Part 5 — CI/CD from GitHub

Two workflows. `ci.yml` runs on every push and pull request; `deploy.yml`
builds images on `main` and rolls them out on a tag or a click.

**Production never builds.** The gateway image compiles the traffic module from
source — minutes of work that can fail — so it is built once in CI and pulled
as an artefact. The thing that was tested is the thing that ships, and a
rollback is pointing at an older tag rather than rebuilding an older commit.

## One-time server setup

On a fresh Ubuntu box, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/shuv-o/cloudbalancer/main/deploy/provision-ubuntu.sh \
  | sudo bash
```

That installs Docker from Docker's own repository, creates a `deploy` user in
the `docker` group, clones the repository to `/opt/cloudbalancer`, opens 22, 80
and 443 in ufw, tunes the kernel for a connection-heavy workload, and turns on
unattended security upgrades.

Then edit `/opt/cloudbalancer/.env` — every value marked CHANGE ME.

## The deploy key

Generate a key **for this purpose only**, on your own machine:

```bash
ssh-keygen -t ed25519 -f ./cloudbalancer-deploy -C "github-actions" -N ""
```

Put the public half on the server:

```bash
ssh-copy-id -i ./cloudbalancer-deploy.pub deploy@your-server
```

Then collect the host's fingerprint, so CI is not trusting whatever answers:

```bash
ssh-keyscan -t ed25519 your-server
```

## Repository secrets

`Settings → Secrets and variables → Actions`:

| Secret | Value |
|---|---|
| `DEPLOY_SSH_KEY` | The **private** half of `cloudbalancer-deploy`, whole file |
| `DEPLOY_HOST` | The server's address |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_KNOWN_HOSTS` | The `ssh-keyscan` output |

And one variable, under the same settings page:

| Variable | Value |
|---|---|
| `PANEL_URL` | Where the panel lives, for the deploy summary link |

No registry credentials are needed. `GITHUB_TOKEN` is issued per run and is
enough to push to `ghcr.io/shuv-o`.

> The `deploy` user is in the `docker` group, which on any Linux host is
> equivalent to root. There is no way around that for a Docker-based deploy, so
> treat `DEPLOY_SSH_KEY` exactly as you would a root key: this repository only,
> nowhere else, rotated if a collaborator leaves.

## The production environment

`Settings → Environments → New environment → production`.

The deploy job targets it, so protection rules are a repository setting rather
than a change to the workflow. Worth adding at least:

- **Required reviewers** — a human approves before the gateway changes
- **Deployment branches** — tags matching `v*` only

## Releasing

```bash
git tag -a v1.0.0 -m "First production release"
git push origin v1.0.0
```

Or `Actions → Deploy → Run workflow` to ship any ref by hand.

A push to `main` builds and pushes images but does not deploy. That is
deliberate: a proxy in front of everything is the wrong place for push-to-main.
If you want it anyway, the condition on the deploy job is one line:

```yaml
# if: startsWith(github.ref, 'refs/tags/v') || github.event_name == 'workflow_dispatch'
if: github.ref == 'refs/heads/main' || github.event_name == 'workflow_dispatch'
```

## What a deploy does

`deploy/deploy.sh`, on the server:

1. **Pull** the new images. The slow part, and nothing is disturbed while it
   runs.
2. **Migrate**, before the new code serves anything. A migration failure stops
   the deploy rather than leaving a container restart-looping.
3. **Rebuild the panel bundle** into its volume.
4. **Start** the new containers. Compose recreates only what changed.
5. **Verify** — `nginx -t` passes and `/health` answers, retried for a minute.
6. **Roll back** to the previous tag if verification fails, then verify that
   too. The tag it rolled back to is recorded in `.deployed-tag`.

## What CI checks

| Job | What it catches |
|---|---|
| **Backend tests** | The full suite, plus a missing-migration check |
| **Panel build** | Typecheck, build, and **any third-party origin in the bundle** — which the panel's CSP would block in a browser, after deployment |
| **Nginx accepts the generated config** | See below |
| **Compose and scripts** | Compose validity, shellcheck, no tracked secrets, no tracked generated config |

The third one is the most valuable. Everything else in this repository asserts
against strings, which catches a missing directive but not one Nginx rejects.
That job builds the real gateway image, renders a deliberately awkward topology
— TLS with and without a redirect, header routing, a regex location, a
streaming route, a published panel with mTLS, a blocked range — and runs
`nginx -t` against it.

The fixture lives in `deploy/ci_fixture.py`. When you add a feature that
changes the generated configuration, add it there too; otherwise the check
keeps passing while covering less.

## Not deployed by this

Deliberately left out of CI's reach:

- **`.env`** on the server. Secrets are not in the repository and not in the
  pipeline, so a deploy cannot rotate them and a leaked pipeline cannot read
  them. Change them over SSH.
- **Certificates.** They live in a Docker volume and renew on their own
  schedule.
- **Database contents.** Domains, backends and routes are operational state,
  not code. They survive every deploy, which is the point of the database being
  the source of truth.

## Zero downtime, honestly

There isn't any, on a single box. Recreating the Nginx container is a few
seconds where connections are refused — Compose only recreates it when its
image or configuration actually changed, so most deploys touch only the control
plane and the gateway keeps serving throughout.

If those seconds matter, you need a second gateway and a load balancer or a
health-checked DNS record in front of the pair. That is the same conclusion as
the single-point-of-failure trade-off in the README, reached from a different
direction.
