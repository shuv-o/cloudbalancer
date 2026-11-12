"""
Monitoring selectors — read operations for the dashboard.

Three sources, each answering a different question:

  * the database      — what is configured, and what the health prober saw
  * Nginx's traffic-status module — what is happening right now, live counters
  * Prometheus        — what happened over time, for the charts

`stub_status` is deliberately not one of them. It reports connections and a
request total and nothing else: no cache statuses, no per-vhost breakdown, no
upstream timing. Cache hit ratio, the number this dashboard exists to show,
cannot be derived from it.
"""
import logging
import re
from datetime import timedelta

import httpx
from django.conf import settings
from django.core.cache import cache
from django.db.models import Avg, Count, Q
from django.utils import timezone

from apps.backends.models import Backend, BackendInstance
from apps.certificates.selectors import certificate_expiry_summary
from apps.domains.models import Domain
from apps.routing.models import ConfigDeployLog, RoutingRule

logger = logging.getLogger(__name__)

CACHE_STATUSES = (
    "hit", "miss", "bypass", "expired", "stale", "updating", "revalidated", "scarce",
)


# ---------------------------------------------------------------------------
# Live counters from Nginx
# ---------------------------------------------------------------------------

_VTS_CACHE_KEY = "monitoring:vts_raw"


def vts_raw() -> dict | None:
    """
    Nginx's traffic-status JSON, or None if it is unreachable.

    Cached for a couple of seconds. The overview endpoint derives traffic,
    cache and upstream figures from this one document, and without a cache it
    fetched the same thing three times per request -- three round trips, three
    timeouts. When Nginx was unreachable that was six seconds of a worker held
    open, on an endpoint the dashboard polls every five.

    The failure is cached too, deliberately: a gateway that cannot answer will
    not answer any faster for being asked three times in a row.
    """
    cached = cache.get(_VTS_CACHE_KEY)
    if cached is not None:
        return cached.get("payload")

    try:
        response = httpx.get(settings.NGINX_VTS_URL, timeout=settings.NGINX_VTS_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("Traffic status unavailable at %s: %s", settings.NGINX_VTS_URL, exc)
        payload = None

    cache.set(_VTS_CACHE_KEY, {"payload": payload}, timeout=settings.NGINX_VTS_CACHE_SECONDS)
    return payload


def _ratio(hits: int, total: int) -> float | None:
    return round(hits / total * 100, 2) if total else None


def _unreachable(payload: dict) -> dict:
    """
    A complete response with nothing in it.

    Returning a short dict when Nginx is unreachable would make every caller
    guard every field, and the first one to forget breaks. The shape stays the
    same whether or not the counters answered; only `available` changes.
    """
    return {
        "available": False,
        "reason": f"Nginx traffic status is not reachable at {settings.NGINX_VTS_URL}.",
        **payload,
    }


def cache_stats() -> dict:
    """
    Cache effectiveness, live.

    `hit_ratio` counts only requests the cache could have served: bypassed
    requests (authenticated ones, mostly) are reported separately rather than
    dragged into the denominator, where they would make a healthy cache look
    broken.
    """
    raw = vts_raw()
    if raw is None:
        return _unreachable({
            "hit_ratio": None,
            "served_from_cache": 0,
            "cacheable_requests": 0,
            "bypassed": 0,
            "breakdown": {status: 0 for status in CACHE_STATUSES},
            "disk_used_bytes": 0,
            "disk_max_bytes": 0,
            "disk_used_pct": None,
            "bytes_from_backend": 0,
            "bytes_to_clients": 0,
            "bytes_saved": 0,
        })

    zones = raw.get("cacheZones", {}) or {}
    totals = {status: 0 for status in CACHE_STATUSES}
    used_size = max_size = in_bytes = out_bytes = 0

    for zone in zones.values():
        responses = zone.get("responses", {}) or {}
        for status in CACHE_STATUSES:
            totals[status] += responses.get(status, 0)
        used_size += zone.get("usedSize", 0)
        max_size += zone.get("maxSize", 0)
        in_bytes += zone.get("inBytes", 0)
        out_bytes += zone.get("outBytes", 0)

    served_from_cache = totals["hit"] + totals["stale"] + totals["revalidated"]
    cacheable = served_from_cache + totals["miss"] + totals["expired"]

    return {
        "available": True,
        "hit_ratio": _ratio(served_from_cache, cacheable),
        "served_from_cache": served_from_cache,
        "cacheable_requests": cacheable,
        "bypassed": totals["bypass"],
        "breakdown": totals,
        "disk_used_bytes": used_size,
        "disk_max_bytes": max_size,
        "disk_used_pct": _ratio(used_size, max_size),
        "bytes_from_backend": in_bytes,
        "bytes_to_clients": out_bytes,
        "bytes_saved": max(out_bytes - in_bytes, 0),
    }


def traffic_stats() -> dict:
    """Per-domain request counts, response classes and mean latency, live."""
    raw = vts_raw()
    if raw is None:
        return _unreachable({
            "uptime_seconds": 0,
            "nginx_version": "",
            "connections": {
                "active": 0, "reading": 0, "writing": 0, "waiting": 0,
                "accepted": 0, "handled": 0, "total_requests": 0,
            },
            "total_requests": 0,
            "domains": [],
        })

    connections = raw.get("connections", {}) or {}
    zones = raw.get("serverZones", {}) or {}

    domains = []
    for name, zone in zones.items():
        if name in ("*", "_"):
            continue
        responses = zone.get("responses", {}) or {}
        requests = zone.get("requestCounter", 0)
        errors = responses.get("4xx", 0) + responses.get("5xx", 0)
        domains.append({
            "domain": name,
            "requests": requests,
            "bytes_in": zone.get("inBytes", 0),
            "bytes_out": zone.get("outBytes", 0),
            "avg_response_ms": zone.get("requestMsec", 0),
            "responses": {
                "1xx": responses.get("1xx", 0),
                "2xx": responses.get("2xx", 0),
                "3xx": responses.get("3xx", 0),
                "4xx": responses.get("4xx", 0),
                "5xx": responses.get("5xx", 0),
            },
            "error_rate": _ratio(errors, requests),
            "cache_hit_ratio": _ratio(
                responses.get("hit", 0) + responses.get("stale", 0),
                sum(responses.get(s, 0) for s in CACHE_STATUSES),
            ),
        })

    domains.sort(key=lambda d: d["requests"], reverse=True)
    total_requests = sum(d["requests"] for d in domains)

    return {
        "available": True,
        "uptime_seconds": round(
            (raw.get("nowMsec", 0) - raw.get("loadMsec", 0)) / 1000
        ),
        "nginx_version": raw.get("nginxVersion", ""),
        "connections": {
            "active": connections.get("active", 0),
            "reading": connections.get("reading", 0),
            "writing": connections.get("writing", 0),
            "waiting": connections.get("waiting", 0),
            "accepted": connections.get("accepted", 0),
            "handled": connections.get("handled", 0),
            "total_requests": connections.get("requests", 0),
        },
        "total_requests": total_requests,
        "domains": domains,
    }


def upstream_stats() -> dict:
    """
    Per-instance traffic and latency, as Nginx sees it.

    This is the view that matters when an instance is misbehaving: the health
    prober reports whether a instance answers a probe, while these counters
    report what real traffic actually experienced.
    """
    raw = vts_raw()
    if raw is None:
        return _unreachable({"upstreams": []})

    upstreams = []
    for upstream_name, servers in (raw.get("upstreamZones", {}) or {}).items():
        if upstream_name.startswith("::"):
            continue
        members = []
        for server in servers:
            responses = server.get("responses", {}) or {}
            requests = server.get("requestCounter", 0)
            errors = responses.get("5xx", 0)
            members.append({
                "server": server.get("server", ""),
                "requests": requests,
                "avg_response_ms": server.get("responseMsec", 0),
                "bytes_in": server.get("inBytes", 0),
                "bytes_out": server.get("outBytes", 0),
                "down": server.get("down", False),
                "weight": server.get("weight", 1),
                "error_rate": _ratio(errors, requests),
                "responses": {
                    "2xx": responses.get("2xx", 0),
                    "4xx": responses.get("4xx", 0),
                    "5xx": responses.get("5xx", 0),
                },
            })
        upstreams.append({
            "upstream": upstream_name,
            "requests": sum(m["requests"] for m in members),
            "servers": members,
        })

    upstreams.sort(key=lambda u: u["requests"], reverse=True)
    return {"available": True, "upstreams": upstreams}


# ---------------------------------------------------------------------------
# Time series from Prometheus
# ---------------------------------------------------------------------------

class SeriesFilterError(ValueError):
    """The caller asked for a domain that cannot be charted, with a reason."""


# Resolution by range length: (range is at most N minutes, step, rate window).
#
# Prometheus refuses a range query asking for more than 11,000 points, so a
# year at 30s -- 1,051,200 points -- is not a slow query, it is a 422. Each row
# here lands between 120 and 400 points, which is more than a chart can draw
# anyway.
#
# The rate window is four times the step, never under two minutes. A window
# narrower than the step samples a thin slice and skips the rest: rate([2m])
# read once a day reports whatever those two minutes held and calls it a year.
SERIES_RESOLUTIONS = (
    (60, "30s", "2m"),               # an hour
    (6 * 60, "2m", "8m"),            # six hours
    (24 * 60, "5m", "20m"),          # a day
    (7 * 24 * 60, "30m", "2h"),      # a week
    (31 * 24 * 60, "2h", "8h"),      # a month
    (92 * 24 * 60, "6h", "24h"),     # a quarter
    (366 * 24 * 60, "1d", "4d"),     # a year
)

MAX_SERIES_MINUTES = SERIES_RESOLUTIONS[-1][0]

# What the range picker offers. Served to the panel rather than hard-coded in
# it, so the longest range on offer cannot outlive what Prometheus retains.
SERIES_RANGES = (
    {"label": "1 hour", "minutes": 60},
    {"label": "6 hours", "minutes": 6 * 60},
    {"label": "24 hours", "minutes": 24 * 60},
    {"label": "7 days", "minutes": 7 * 24 * 60},
    {"label": "30 days", "minutes": 30 * 24 * 60},
    {"label": "90 days", "minutes": 90 * 24 * 60},
    {"label": "1 year", "minutes": 365 * 24 * 60},
)

# A label value is interpolated into PromQL, so it is checked rather than
# escaped. Nothing that is not a hostname gets through, which leaves no quote
# or brace to close the selector with.
#
# The leading `*.` is allowed because a wildcard is a real Domain here, and the
# traffic module labels its zone with the literal server_name -- so `*` is what
# Prometheus holds and what an exact match has to ask for. It is safe in a
# label value: `=` is equality, not a pattern, and a value cannot contain a
# quote or a brace to escape the selector with.
HOSTNAME = re.compile(r"^(?:\*\.)?[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?$")
UPSTREAM_NAME = re.compile(r"^[A-Za-z0-9_]+$")


def resolution_for(minutes: int) -> tuple[str, str]:
    """The step and rate window a range of this length can actually be drawn at."""
    for limit, step, window in SERIES_RESOLUTIONS:
        if minutes <= limit:
            return step, window
    return SERIES_RESOLUTIONS[-1][1], SERIES_RESOLUTIONS[-1][2]


def upstreams_serving(host: str) -> list[str]:
    """
    The Nginx upstream blocks this domain's routes send traffic to.

    Needed because the traffic module labels upstream counters with the
    upstream and the backend address and nothing else -- there is no host label
    to filter on. The link from a domain to its upstreams exists only in the
    database, which is the same place the config generator reads it from.
    """
    names = (
        RoutingRule.objects
        .filter(domain__name=host, is_active=True)
        .values_list("backend__name", flat=True)
        .distinct()
    )
    upstreams = sorted({
        name.replace("-", "_").replace(".", "_") for name in names if name
    })
    return [name for name in upstreams if UPSTREAM_NAME.match(name)]


def _prometheus_range(query: str, *, minutes: int, step: str) -> list[dict]:
    """Run one range query and flatten it into series the charts can read."""
    end = timezone.now()
    start = end - timedelta(minutes=minutes)

    try:
        response = httpx.get(
            f"{settings.PROMETHEUS_URL}/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
            timeout=settings.PROMETHEUS_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        logger.warning("Prometheus query failed (%s): %s", query, exc)
        return []

    if payload.get("status") != "success":
        return []

    series = []
    for result in payload["data"].get("result", []):
        series.append({
            "labels": result.get("metric", {}),
            "points": [
                {"t": float(ts), "v": float(value) if value != "NaN" else None}
                for ts, value in result.get("values", [])
            ],
        })
    return series


def traffic_series(
    *,
    minutes: int = 60,
    host: str | None = None,
) -> dict:
    """
    The five series the dashboard charts, for every domain or for one.

    `gateway_added_ms` is the one worth watching. Nginx records total request
    time and upstream response time separately, so subtracting them and
    dividing by request count gives the gateway's own overhead in milliseconds
    -- measured continuously, in production, with no load generator involved.
    That is the number the sub-millisecond budget is about.

    With no `host`, every per-domain query excludes host="*", the traffic
    module's own aggregate row, and host="_", the catch-all server. The first
    would double every total; the second is not a configured domain, answers
    without an upstream, and would therefore charge its whole response time to
    the gateway's overhead. The live per-domain table drops both for the same
    reasons.

    With a `host`, two of the five change source rather than just gaining a
    filter, because the traffic module does not label everything by vhost:

      * cache moves from nginx_vts_cache_requests_total, which is per cache
        zone and shared between domains, to nginx_vts_server_cache_total,
        which is per vhost;
      * upstream timings carry no host label at all, so the domain is resolved
        to its upstreams through the routing rules first. A domain with no
        active route has no upstream to chart, and says so.

    The code label is matched as a class pattern, never as code="total". The
    exporter writes one row per class -- 1xx, 2xx, 3xx, 4xx, 5xx -- and emits
    no aggregate row at all, so a filter on "total" selects nothing. An empty
    selector stays empty through clamp_min() and through division, so the
    mistake does not surface as a zero. It surfaces as a blank chart, which
    looks exactly like a gateway with no traffic.
    """
    minutes = max(1, min(int(minutes), MAX_SERIES_MINUTES))
    step, window = resolution_for(minutes)

    if host:
        if not HOSTNAME.match(host):
            raise SeriesFilterError(f"{host!r} is not a hostname.")
        if not Domain.objects.filter(name=host).exists():
            raise SeriesFilterError(f"No domain named {host} is configured.")
        server = f'host="{host}"'
        upstreams = upstreams_serving(host)
        # An empty upstream selector would match every upstream, which is the
        # opposite of filtering. Matching the empty string matches none.
        upstream = 'upstream=~"' + ("|".join(upstreams) or "") + '"'
        cache_metric = "nginx_vts_server_cache_total"
        cache_scope = f'host="{host}",'
    else:
        server = 'host!="*",host!="_"'
        upstreams = []
        upstream = ""
        cache_metric = "nginx_vts_cache_requests_total"
        cache_scope = ""

    def _range(query: str) -> list[dict]:
        return _prometheus_range(query, minutes=minutes, step=step)

    requests = (
        f'sum(rate(nginx_vts_server_requests_total{{{server},code=~"[1-5]xx"}}[{window}]))'
    )
    # `or vector(0)` is not papering over a missing series. A domain served
    # entirely from cache reaches no upstream, so the counter genuinely does
    # not exist, and without this the gateway-overhead chart would go blank at
    # exactly the moment the cache started working.
    upstream_seconds = (
        f"(sum(rate(nginx_vts_upstream_response_seconds_total{{{upstream}}}[{window}]))"
        " or vector(0))"
    )

    return {
        "range_minutes": minutes,
        "step": step,
        "rate_window": window,
        "domain": host or "",
        "upstreams": upstreams,
        "requests_per_second": _range(requests),
        "gateway_added_ms": _range(
            "("
            f"sum(rate(nginx_vts_server_request_seconds_total{{{server}}}[{window}]))"
            f" - {upstream_seconds}"
            f") / clamp_min({requests}, 1) * 1000"
        ),
        "cache_hit_ratio": _range(
            f"sum(rate({cache_metric}"
            f'{{{cache_scope}status=~"hit|stale|revalidated"}}[{window}]))'
            f" / clamp_min(sum(rate({cache_metric}"
            f'{{{cache_scope}status=~"hit|miss|expired|stale|revalidated"}}[{window}])), 1)'
            " * 100"
        ),
        "upstream_latency_ms": _range(
            f"sum(rate(nginx_vts_upstream_response_seconds_total{{{upstream}}}[{window}]))"
            " by (upstream)"
            f" / clamp_min(sum(rate(nginx_vts_upstream_requests_total"
            f'{{{upstream + "," if upstream else ""}code=~"[1-5]xx"}}[{window}]))'
            " by (upstream), 1) * 1000"
        ),
        "status_classes": _range(
            f'sum(rate(nginx_vts_server_requests_total{{{server},code=~"[2345]xx"}}'
            f"[{window}])) by (code)"
        ),
        # Per domain, over the chosen range.
        #
        # The live per-domain table comes from Nginx's own counters, which
        # live in shared memory and start again from zero whenever the
        # container restarts -- a deploy, a reboot, an image rebuild. That is
        # correct for "what is happening now" and reads as data loss to
        # anyone looking for a month of history.
        #
        # This is the history. rate() over a window is immune to a counter
        # going back to zero, so a restart leaves no gap and no spike, and
        # the range picker reaches back as far as Prometheus retains.
        # The latency distribution, as a share of requests in each bucket.
        #
        # A mean hides the tail completely: ten thousand requests at 5ms and
        # a hundred at nine seconds average to something that looks healthy,
        # and the hundred are the ones somebody is complaining about. These
        # are cumulative counters -- le="0.1" counts everything at or below
        # 100ms -- so the chart shows what fraction of traffic was served
        # within each boundary.
        "latency_buckets": _range(
            f"sum(rate(nginx_vts_server_request_duration_seconds_bucket"
            f'{{{server}}}[{window}])) by (le)'
            f" / clamp_min(sum(rate(nginx_vts_server_request_duration_seconds_count"
            f'{{{server}}}[{window}])), 1) * 100'
        ),
        "requests_by_domain": _range(
            f'sum(rate(nginx_vts_server_requests_total{{{server},code=~"[1-5]xx"}}'
            f"[{window}])) by (host)"
        ),
    }


# ---------------------------------------------------------------------------
# Database-backed summaries
# ---------------------------------------------------------------------------

def dashboard_summary() -> dict:
    """
    Everything the dashboard header needs.

    One aggregate per table rather than one per number. The page polls this
    every few seconds, and a dozen separate COUNT round trips to answer
    questions about the same four tables is a dozen more than it needs.
    """
    now = timezone.now()
    last_24h = now - timedelta(hours=24)

    domains = Domain.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        ssl_enabled=Count("id", filter=Q(ssl_enabled=True)),
    )

    backends = Backend.objects.aggregate(
        total=Count("id", filter=Q(is_active=True)),
    )

    instances = BackendInstance.objects.filter(is_active=True).aggregate(
        total=Count("id"),
        healthy=Count("id", filter=Q(is_healthy=True)),
        draining=Count("id", filter=Q(is_draining=True)),
        avg_probe_ms=Avg("last_health_response_ms"),
    )

    rules = RoutingRule.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        cached=Count("id", filter=Q(is_active=True, cache_enabled=True)),
    )

    deploys = ConfigDeployLog.objects.filter(created_at__gte=last_24h).aggregate(
        total=Count("id"),
        failed=Count("id", filter=Q(status=ConfigDeployLog.Status.FAILED)),
    )

    return {
        "domains": {
            "total": domains["total"],
            "active": domains["active"],
            "ssl_enabled": domains["ssl_enabled"],
        },
        "backends": {
            "total": backends["total"],
            "instances_total": instances["total"],
            "instances_healthy": instances["healthy"],
            "instances_draining": instances["draining"],
            "avg_probe_ms": instances["avg_probe_ms"],
        },
        "routing": {
            "total_rules": rules["total"],
            "active_rules": rules["active"],
            "cached_rules": rules["cached"],
        },
        "certificates": certificate_expiry_summary(),
        "deploys": {
            "last_24h_total": deploys["total"],
            "last_24h_failed": deploys["failed"],
            "last_deploy": ConfigDeployLog.objects.values(
                "id", "status", "created_at", "completed_at"
            ).first(),
        },
    }


def backend_health_grid() -> list[dict]:
    """Per-instance health, the view the backends page is built around."""
    grid = []
    for backend in Backend.objects.filter(is_active=True).prefetch_related("instances"):
        instances = [i for i in backend.instances.all() if i.is_active]
        healthy = sum(1 for i in instances if i.is_healthy)
        grid.append({
            "id": backend.id,
            "name": backend.name,
            "lb_method": backend.lb_method,
            "drain_unhealthy": backend.drain_unhealthy,
            "instances": [
                {
                    "id": i.id,
                    "address": i.netloc,
                    "state": i.state,
                    "is_healthy": i.is_healthy,
                    "is_draining": i.is_draining,
                    "weight": i.weight,
                    "consecutive_failures": i.consecutive_failures,
                    "last_check": i.last_health_check,
                    "response_ms": i.last_health_response_ms,
                    "status_code": i.last_health_status_code,
                }
                for i in instances
            ],
            "healthy_count": healthy,
            "total_count": len(instances),
            "status": (
                "healthy" if instances and healthy == len(instances)
                else "degraded" if healthy
                else "down"
            ),
        })
    return grid
