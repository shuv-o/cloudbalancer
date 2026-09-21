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
git clone <your-repo> /opt/proxy-balancer
cd /opt/proxy-balancer
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

If you would rather reach it over a LAN or VPN address, set `PANEL_BIND` to that
address in `.env` — not to `0.0.0.0`, and never without a firewall in front.

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
docker compose exec -T postgres pg_dump -U proxy_balancer proxy_balancer \
  | gzip > backup-$(date +%F).sql.gz

# Certificates and ACME account keys.
docker run --rm -v proxy_balancer_letsencrypt:/data -v "$PWD":/out alpine \
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
