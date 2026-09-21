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
from datetime import timedelta

import httpx
from django.conf import settings
from django.db.models import Avg
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

def vts_raw() -> dict | None:
    """Fetch Nginx's traffic-status JSON, or None if it is unreachable."""
    try:
        response = httpx.get(settings.NGINX_VTS_URL, timeout=settings.NGINX_VTS_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.warning("Traffic status unavailable at %s: %s", settings.NGINX_VTS_URL, exc)
        return None


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


def traffic_series(*, minutes: int = 60, step: str = "30s") -> dict:
    """
    The four series the dashboard charts.

    `gateway_added_ms` is the one worth watching. Nginx records total request
    time and upstream response time separately, so subtracting them and
    dividing by request count gives the gateway's own overhead in milliseconds
    -- measured continuously, in production, with no load generator involved.
    That is the number the sub-millisecond budget is about.

    Every query excludes host="*", which is the traffic module's own aggregate
    row; counting it alongside the real vhosts would double every total.
    """
    return {
        "range_minutes": minutes,
        "step": step,
        "requests_per_second": _prometheus_range(
            'sum(rate(nginx_vts_server_requests_total{host!="*",code="total"}[2m]))',
            minutes=minutes, step=step,
        ),
        "gateway_added_ms": _prometheus_range(
            "("
            '  sum(rate(nginx_vts_server_request_seconds_total{host!="*"}[2m]))'
            "  - sum(rate(nginx_vts_upstream_response_seconds_total[2m]))"
            ") / clamp_min("
            '  sum(rate(nginx_vts_server_requests_total{host!="*",code="total"}[2m])), 1'
            ") * 1000",
            minutes=minutes, step=step,
        ),
        "cache_hit_ratio": _prometheus_range(
            'sum(rate(nginx_vts_cache_total{status="hit"}[5m])) / clamp_min('
            'sum(rate(nginx_vts_cache_total{status=~"hit|miss|expired|stale|revalidated"}[5m])), 1'
            ") * 100",
            minutes=minutes, step=step,
        ),
        "upstream_latency_ms": _prometheus_range(
            "sum(rate(nginx_vts_upstream_response_seconds_total[2m])) by (upstream) / "
            'clamp_min(sum(rate(nginx_vts_upstream_requests_total{code="total"}[2m])) by (upstream), 1) '
            "* 1000",
            minutes=minutes, step=step,
        ),
        "status_classes": _prometheus_range(
            'sum(rate(nginx_vts_server_requests_total{host!="*",code=~"[2345]xx"}[2m])) by (code)',
            minutes=minutes, step=step,
        ),
    }


# ---------------------------------------------------------------------------
# Database-backed summaries
# ---------------------------------------------------------------------------

def dashboard_summary() -> dict:
    """Everything the dashboard header needs, in one query set."""
    now = timezone.now()
    last_24h = now - timedelta(hours=24)

    domains = Domain.objects.all()
    backends = Backend.objects.filter(is_active=True)
    instances = BackendInstance.objects.filter(is_active=True)
    rules = RoutingRule.objects.all()
    recent_deploys = ConfigDeployLog.objects.filter(created_at__gte=last_24h)

    return {
        "domains": {
            "total": domains.count(),
            "active": domains.filter(is_active=True).count(),
            "ssl_enabled": domains.filter(ssl_enabled=True).count(),
        },
        "backends": {
            "total": backends.count(),
            "instances_total": instances.count(),
            "instances_healthy": instances.filter(is_healthy=True).count(),
            "instances_draining": instances.filter(is_draining=True).count(),
            "avg_probe_ms": instances.filter(
                last_health_response_ms__isnull=False
            ).aggregate(avg=Avg("last_health_response_ms"))["avg"],
        },
        "routing": {
            "total_rules": rules.count(),
            "active_rules": rules.filter(is_active=True).count(),
            "cached_rules": rules.filter(is_active=True, cache_enabled=True).count(),
        },
        "certificates": certificate_expiry_summary(),
        "deploys": {
            "last_24h_total": recent_deploys.count(),
            "last_24h_failed": recent_deploys.filter(
                status=ConfigDeployLog.Status.FAILED
            ).count(),
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
