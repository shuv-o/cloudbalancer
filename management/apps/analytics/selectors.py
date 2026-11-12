"""
Reading the rollups back.

Every function here takes a window in days and an optional domain, because
that is the shape of every question the dashboards ask: who, from where, what,
and when -- for this domain, over this period.

The windows are bounded by what each table retains, which differs. Asking for
a year of client addresses returns thirty days of them and says so, rather
than returning a short answer that looks complete.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, F, Max, Q, Sum
from django.utils import timezone

from apps.analytics.models import AgentDay, ClientDay, GeoDay, HourBucket, LogCursor, PathDay

# What the panel offers. Each is clamped per table to whatever that table
# still holds, so a range is never silently truncated without a word.
RANGES = (
    {"label": "24 hours", "days": 1},
    {"label": "7 days", "days": 7},
    {"label": "30 days", "days": 30},
    {"label": "90 days", "days": 90},
    {"label": "1 year", "days": 365},
)


def _window(days: int, limit: int) -> tuple:
    """The date range actually available, and whether it was shortened."""
    days = max(1, min(int(days), 400))
    effective = min(days, limit)
    today = timezone.now().date()
    return today - timedelta(days=effective - 1), today, effective, effective < days


def _scope(queryset, domain: str | None, start, end):
    queryset = queryset.filter(date__gte=start, date__lte=end)
    if domain:
        queryset = queryset.filter(domain=domain)
    return queryset


def summary(*, days: int = 7, domain: str | None = None) -> dict:
    """The header: what this window contains, and whether ingestion is working."""
    start, end, effective, _ = _window(days, settings.ANALYTICS_AGGREGATE_RETENTION_DAYS)

    hours = _scope(HourBucket.objects.all(), domain, start, end).aggregate(
        requests=Sum("requests"),
        bytes_sent=Sum("bytes_sent"),
        errors=Sum("errors"),
        total_ms=Sum("total_ms"),
    )
    requests = hours["requests"] or 0

    geo = _scope(GeoDay.objects.all(), domain, start, end)
    countries = geo.exclude(country="").values("country").distinct().count()
    networks = geo.exclude(asn__isnull=True).values("asn").distinct().count()

    # Clients only go back as far as the addresses are kept.
    client_start, _, client_days, _ = _window(
        days, settings.ANALYTICS_CLIENT_RETENTION_DAYS
    )
    clients = (
        _scope(ClientDay.objects.all(), domain, client_start, end)
        .values("ip")
        .distinct()
        .count()
    )

    cursors = LogCursor.objects.all()
    read = cursors.aggregate(
        lines=Sum("lines_read"), unparsed=Sum("lines_unparsed"), last=Max("last_read_at")
    )

    return {
        "days": effective,
        "domain": domain or "",
        "requests": requests,
        "bytes_sent": hours["bytes_sent"] or 0,
        "errors": hours["errors"] or 0,
        "error_rate": round((hours["errors"] or 0) / requests * 100, 2) if requests else None,
        "average_ms": round((hours["total_ms"] or 0) / requests, 1) if requests else None,
        "distinct_clients": clients,
        "client_days": client_days,
        "countries": countries,
        "networks": networks,
        "ingest": {
            "files": cursors.count(),
            "lines": read["lines"] or 0,
            "unparsed": read["unparsed"] or 0,
            "last_read_at": read["last"],
        },
    }


def top_clients(*, days: int = 7, domain: str | None = None, limit: int = 50) -> dict:
    """
    The busiest addresses, and where they are.

    Bounded by the retention on addresses rather than on aggregates, and the
    response says which, because "no clients in the last year" and "we only
    keep thirty days of addresses" are different answers.
    """
    start, end, effective, shortened = _window(
        days, settings.ANALYTICS_CLIENT_RETENTION_DAYS
    )

    rows = (
        _scope(ClientDay.objects.all(), domain, start, end)
        .values("ip")
        .annotate(
            requests=Sum("requests"),
            bytes_sent=Sum("bytes_sent"),
            errors=Sum("errors"),
            server_errors=Sum("server_errors"),
            total_ms=Sum("total_ms"),
            last_seen=Max("last_seen"),
            domains=Count("domain", distinct=True),
            country=Max("country"),
            country_name=Max("country_name"),
            city=Max("city"),
            asn=Max("asn"),
            asn_name=Max("asn_name"),
            latitude=Max("latitude"),
            longitude=Max("longitude"),
        )
        .order_by("-requests")[:limit]
    )

    clients = []
    for row in rows:
        requests = row["requests"] or 0
        clients.append({
            **row,
            "average_ms": round((row["total_ms"] or 0) / requests, 1) if requests else None,
            "error_rate": round((row["errors"] or 0) / requests * 100, 1) if requests else None,
        })

    return {
        "days": effective,
        "truncated_to_retention": shortened,
        "retention_days": settings.ANALYTICS_CLIENT_RETENTION_DAYS,
        "clients": clients,
    }


def geography(*, days: int = 7, domain: str | None = None, limit: int = 100) -> dict:
    """Traffic by country and by network, for the map and the table beside it."""
    start, end, effective, _ = _window(days, settings.ANALYTICS_AGGREGATE_RETENTION_DAYS)
    scoped = _scope(GeoDay.objects.all(), domain, start, end)

    countries = list(
        scoped.exclude(country="")
        .values("country")
        .annotate(
            country_name=Max("country_name"),
            requests=Sum("requests"),
            bytes_sent=Sum("bytes_sent"),
            errors=Sum("errors"),
            clients=Sum("clients"),
        )
        .order_by("-requests")[:limit]
    )

    networks = list(
        scoped.exclude(asn__isnull=True)
        .values("asn")
        .annotate(
            asn_name=Max("asn_name"),
            country=Max("country"),
            requests=Sum("requests"),
            bytes_sent=Sum("bytes_sent"),
            errors=Sum("errors"),
            clients=Sum("clients"),
        )
        .order_by("-requests")[:limit]
    )

    unknown = scoped.filter(country="").aggregate(requests=Sum("requests"))["requests"] or 0

    return {
        "days": effective,
        "countries": countries,
        "networks": networks,
        # Private addresses, and anything the database has not heard of.
        # Shown rather than dropped: a map that silently omits a third of the
        # traffic is worse than one that says so.
        "unlocated_requests": unknown,
    }


def paths(*, days: int = 7, domain: str | None = None, limit: int = 50) -> dict:
    """Busiest, slowest and most broken, which are rarely the same paths."""
    start, end, effective, shortened = _window(
        days, settings.ANALYTICS_PATH_RETENTION_DAYS
    )
    scoped = _scope(PathDay.objects.all(), domain, start, end)

    grouped = scoped.values("path").annotate(
        requests=Sum("requests"),
        bytes_sent=Sum("bytes_sent"),
        errors=Sum("errors"),
        server_errors=Sum("server_errors"),
        cache_hits=Sum("cache_hits"),
        total_ms=Sum("total_ms"),
        worst_ms=Max("max_ms"),
    )

    def decorate(rows):
        out = []
        for row in rows:
            requests = row["requests"] or 0
            out.append({
                **row,
                "average_ms": round((row["total_ms"] or 0) / requests, 1) if requests else None,
                "error_rate": round((row["errors"] or 0) / requests * 100, 1) if requests else None,
                "cache_hit_ratio": (
                    round((row["cache_hits"] or 0) / requests * 100, 1) if requests else None
                ),
            })
        return out

    busiest = decorate(grouped.order_by("-requests")[:limit])

    # Sorted by time per request rather than total time: a path served a
    # million times at 2ms would otherwise outrank the one that takes nine
    # seconds, which is the one somebody needs to see.
    slowest = sorted(
        (row for row in decorate(grouped[:500]) if (row["requests"] or 0) >= 5),
        key=lambda row: row["average_ms"] or 0,
        reverse=True,
    )[:limit]

    failing = sorted(
        (row for row in decorate(grouped[:500]) if (row["errors"] or 0) > 0),
        key=lambda row: row["errors"],
        reverse=True,
    )[:limit]

    return {
        "days": effective,
        "truncated_to_retention": shortened,
        "retention_days": settings.ANALYTICS_PATH_RETENTION_DAYS,
        "busiest": busiest,
        "slowest": slowest,
        "failing": failing,
    }


def agents(*, days: int = 7, domain: str | None = None) -> dict:
    """Browser against crawler against script, which changes what a spike means."""
    start, end, effective, _ = _window(days, settings.ANALYTICS_AGGREGATE_RETENTION_DAYS)

    rows = list(
        _scope(AgentDay.objects.all(), domain, start, end)
        .values("kind")
        .annotate(
            requests=Sum("requests"),
            bytes_sent=Sum("bytes_sent"),
            example=Max("example"),
        )
        .order_by("-requests")
    )

    total = sum(row["requests"] or 0 for row in rows)
    for row in rows:
        row["share"] = round((row["requests"] or 0) / total * 100, 1) if total else 0
        row["label"] = AgentDay.Kind(row["kind"]).label

    return {"days": effective, "total": total, "kinds": rows}


def heatmap(*, days: int = 28, domain: str | None = None) -> dict:
    """
    Requests by weekday and hour.

    Averaged per occurrence rather than summed, so a four-week window and a
    one-week window are comparable -- a summed grid just gets four times
    darker and says nothing new.
    """
    start, end, effective, _ = _window(days, settings.ANALYTICS_AGGREGATE_RETENTION_DAYS)

    totals: dict = {}
    occurrences: dict = {}
    for row in _scope(HourBucket.objects.all(), domain, start, end).values(
        "date", "hour", "requests", "errors", "total_ms"
    ):
        key = (row["date"].weekday(), row["hour"])
        current = totals.setdefault(key, {"requests": 0, "errors": 0, "total_ms": 0.0})
        current["requests"] += row["requests"]
        current["errors"] += row["errors"]
        current["total_ms"] += row["total_ms"]
        occurrences.setdefault(key, set()).add(row["date"])

    cells = []
    for (weekday, hour), values in sorted(totals.items()):
        seen = len(occurrences[(weekday, hour)]) or 1
        requests = values["requests"]
        cells.append({
            "weekday": weekday,
            "hour": hour,
            "requests": requests,
            "average_requests": round(requests / seen, 1),
            "errors": values["errors"],
            "average_ms": round(values["total_ms"] / requests, 1) if requests else None,
        })

    peak = max((cell["average_requests"] for cell in cells), default=0)
    return {"days": effective, "peak": peak, "cells": cells}


def domains() -> list[str]:
    """Every domain the logs have mentioned, which may outlive its config."""
    return sorted(
        set(HourBucket.objects.values_list("domain", flat=True).distinct())
        | set(GeoDay.objects.values_list("domain", flat=True).distinct())
    )
