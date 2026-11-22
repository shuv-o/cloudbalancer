# TODO

Work agreed for this installation but not started. Tick items off as they land,
and move anything that turns out bigger than it looks into its own issue.

---

## 1. High availability — remove the single point of failure

Today every site goes through one machine (`103.159.2.144`). If it goes down,
every domain goes down with it, even though the backends are healthy. The
README lists this as a known trade-off; this is the plan to close it.

**Target design:** two gateways sharing one floating IP (keepalived / VRRP).

```
          DNS: all sites → floating IP
                       │
        ┌──────────────┴──────────────┐
  Server A (.144)                Server B
  gateway: ACTIVE  ◄── health ──► gateway: STANDBY
  + control panel                 (same config + certs, pushed from A)
```

- Failover in ~2–3 seconds, no DNS change, at most one failed request.
- Cost: B's cache starts cold after a failover; hit ratio dips for a few minutes.
- The control panel stays on A. If A dies, sites keep serving from B on their
  last config — only *changing* settings waits for A to come back.

### Open questions (answer before building)

- [ ] Is the second server in the same subnet as `.144` (e.g. `103.159.2.x`)?
      A floating IP only works inside one L2 network. If not: two A records, or
      anycast via BdREN's network team.
- [ ] Can BdREN allocate one extra public IP in that subnet to float?
      (Or `.144` itself becomes the floating address.)
- [ ] Is the second server dedicated? Do **not** use `.210` or any backend
      host — a gateway on a backend fails together with the sites it serves.
- [ ] Is VRRP (IP protocol 112) / multicast allowed between the two hosts, or
      do we need unicast peers?

### Phase 1 — Multi-server config sync (the prerequisite)

The control plane only reloads Nginx on its own machine, through a shared
volume. Everything below depends on fixing that.

- [ ] Register gateway nodes in the panel (name, address, role, last seen).
- [ ] Node agent on each gateway: receives a rendered config bundle, runs
      `nginx -t`, reloads, reports back. Authenticated (mTLS or signed bundles)
      over the private network only.
- [ ] Deploy pushes to **all** nodes; a deploy succeeds only if every node
      passes `nginx -t`, and rolls back everywhere if any fails.
- [ ] Panel shows per-node config version; alert when nodes disagree.

### Phase 2 — Certificates on every node

- [ ] Push certificates and keys to all nodes with each deploy/renewal.
- [ ] ACME HTTP-01: forward `/.well-known/acme-challenge/` from B to A, so
      issuance and renewal work whichever node holds the IP.
- [ ] Panel domain (`cloudbalancer.bdren.xyz`) served from B proxies `/api/`
      to Django on A over the private network.

### Phase 3 — keepalived

- [ ] Provisioning script for the standby (`deploy/provision-standby.sh`).
- [ ] keepalived config: health check = Nginx `/health` on `:8080`,
      `nopreempt` (no flip-flopping back to A), VRRP auth, unicast peers if needed.
- [ ] Firewall rules for VRRP between the two hosts only.

### Phase 4 — Observability

- [ ] Prometheus scrapes both gateways; dashboards split by node.
- [ ] Discord alerts: gateway node unreachable, configs out of sync,
      failover happened (VIP moved).

### Phase 5 — Prove it

- [ ] Failover drill: stop Nginx on A, measure time until B serves; then
      power off A entirely and repeat.
- [ ] Document the drill and the recovery steps in `DEPLOY.md`.

### Later (not needed for the gateway to survive a node loss)

- [ ] Control plane HA: Postgres replication + standby Django.
- [ ] Own authoritative DNS (Knot DNS, 1 primary + 2 secondaries) — scoped
      2026-11-10, depends on Phase 1.

---

## 2. Security decisions

- [ ] **Panel IP allowlist.** `cloudbalancer.bdren.xyz` is reachable from the
      whole internet; only password + 2FA protect control of every site. Add
      the office / VPN ranges in Security → Access policy.
- [ ] **Second admin account.** There is exactly one admin; a lost
      authenticator locks everyone out.
- [ ] **Off-site backups.** Daily encrypted backups exist but sit on the same
      server they protect. Copy them elsewhere daily.
- [ ] **Private uploads in cache.** If any site serves private images/media
      behind a login, store static files only from signed-out requests
      (signed-in users would still be served cached copies).

## 3. Sites

- [ ] **`sms.bdren.net.bd` backend unstable.** The Dokploy backend
      (`103.28.121.54:80`) logged 58 connection resets and 22 connect
      timeouts. Investigate on the Dokploy side.
- [ ] **Research site browser caching scope.** "Let browsers keep static files
      for a year" is on for the whole `/` route. If logos, favicon or uploads
      are ever replaced under the same name, move it to a dedicated `/build/`
      route (priority 10) and turn it off on `/`.
- [ ] **Other sites' `Cache-Control`.** Check phd, postdoc, filebrowser, pma
      and sms for static files sent without a browser cache policy (PageSpeed
      flags this).
- [ ] **"Gateway adds more than 5 ms" alert.** Was firing, likely from large
      upload attempts. Confirm it cleared after the upload-limit fix.

## 4. Housekeeping

- [ ] Local `.env` pins `NGINX_VERSION=1.27.4`; local builds now refuse it.
      Remove the line or set `1.30.7`.
