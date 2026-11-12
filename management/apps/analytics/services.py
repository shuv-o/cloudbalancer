"""
Turning log lines into the five daily rollups, and keeping them trimmed.

The whole pass is: read what is new in each domain's log, fold it into
in-memory counters, and write those counters into the day's rows with one
upsert each. Folding first is what makes it affordable -- a million lines
become a few hundred rows, and the database sees a few hundred writes rather
than a million.

Nothing here writes a raw line anywhere. The log file is the only place one
exists, and it stays Nginx's.
"""
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.analytics import geo
from apps.analytics.models import (
    AgentDay,
    ClientDay,
    GeoDay,
    HourBucket,
    LogCursor,
    PathDay,
)
from apps.analytics.parser import domain_for, log_files, parse_line, read_new_lines

logger = logging.getLogger(__name__)

# Enough of the agent string to tell a crawler from a browser. Order matters:
# plenty of bots claim to be Mozilla, so the bot patterns are tried first.
BOT = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|bingpreview|duckduck|"
    r"yandex|baidu|semrush|ahrefs|mj12|dotbot|petalbot|gptbot|claudebot|"
    r"ccbot|bytespider|applebot|uptime|pingdom|statuscake|monitor",
    re.I,
)
TOOL = re.compile(r"curl|wget|python-requests|httpx|go-http|java/|okhttp|libwww|axios|postman", re.I)
BROWSER = re.compile(r"mozilla|chrome|safari|firefox|edge|opera", re.I)


def classify_agent(agent: str) -> str:
    if not agent or agent == "-":
        return AgentDay.Kind.UNKNOWN
    if BOT.search(agent):
        return AgentDay.Kind.BOT
    if TOOL.search(agent):
        return AgentDay.Kind.TOOL
    if BROWSER.search(agent):
        return AgentDay.Kind.BROWSER
    return AgentDay.Kind.UNKNOWN


def normalise_path(path: str) -> str:
    """
    A path worth counting.

    The query string goes: it turns one endpoint into a row per distinct
    parameter value, which is unbounded and answers nothing /search does not.
    Numeric and UUID segments collapse for the same reason -- a thousand rows
    for /users/1 .. /users/1000 hide the one fact worth having, which is that
    /users/{id} is busy.
    """
    if not path:
        return "/"

    path = path.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/"):
        path = "/" + path

    segments = []
    for segment in path.split("/"):
        if not segment:
            segments.append(segment)
        elif segment.isdigit():
            segments.append("{n}")
        elif len(segment) >= 32 and re.fullmatch(r"[0-9a-fA-F-]{32,}", segment):
            segments.append("{id}")
        else:
            segments.append(segment)

    return "/".join(segments)[:512] or "/"


@dataclass
class _Counts:
    requests: int = 0
    bytes_sent: int = 0
    errors: int = 0
    server_errors: int = 0
    cache_hits: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    last_seen: object = None
    example: str = ""

    def add(self, request) -> None:
        self.requests += 1
        self.bytes_sent += request.bytes_sent
        self.total_ms += request.duration_ms
        self.max_ms = max(self.max_ms, request.duration_ms)
        if request.is_error:
            self.errors += 1
        if request.is_server_error:
            self.server_errors += 1
        if request.cache.lower() in ("hit", "stale", "revalidated"):
            self.cache_hits += 1
        if self.last_seen is None or request.at > self.last_seen:
            self.last_seen = request.at


@dataclass
class _Rollup:
    """Everything one pass accumulates before touching the database."""

    clients: dict = field(default_factory=lambda: defaultdict(_Counts))
    paths: dict = field(default_factory=lambda: defaultdict(_Counts))
    agents: dict = field(default_factory=lambda: defaultdict(_Counts))
    hours: dict = field(default_factory=lambda: defaultdict(_Counts))

    def add(self, domain: str, request) -> None:
        day = request.at.date()
        self.clients[(day, domain, request.ip)].add(request)
        self.paths[(day, domain, normalise_path(request.path))].add(request)
        self.hours[(day, request.at.hour, domain)].add(request)

        kind = classify_agent(request.agent)
        counts = self.agents[(day, domain, kind)]
        counts.add(request)
        if not counts.example:
            counts.example = request.agent[:300]


def ingest(*, directory: Path | None = None) -> dict:
    """
    Read whatever is new and fold it in.

    Returns what it did, which is what the panel shows and what makes a pass
    that read nothing distinguishable from one that failed.
    """
    directory = directory or Path(settings.NGINX_LOG_DIR)
    files = log_files(directory)

    if not files:
        return {
            "files": 0,
            "lines": 0,
            "unparsed": 0,
            "reason": f"no per-domain access logs in {directory}",
        }

    rollup = _Rollup()
    read = unparsed = 0
    budget = settings.ANALYTICS_MAX_LINES_PER_RUN

    for path in files:
        cursor, _ = LogCursor.objects.get_or_create(file_name=path.name)
        lines, offset, inode, digest = read_new_lines(
            path,
            offset=cursor.offset,
            inode=cursor.inode or None,
            digest=cursor.head_digest,
        )

        if budget and len(lines) > budget - read:
            # A pass that tried to swallow a week of backlog in one go would
            # hold a worker for minutes and allocate the lot. Stopping short
            # leaves the cursor behind the end, and the next run continues.
            lines = lines[: max(budget - read, 0)]
            offset = cursor.offset + sum(len(line.encode()) + 1 for line in lines)

        domain = domain_for(path)
        for line in lines:
            request = parse_line(line)
            if request is None:
                unparsed += 1
                continue
            rollup.add(domain, request)

        read += len(lines)
        LogCursor.objects.filter(pk=cursor.pk).update(
            offset=offset,
            inode=inode,
            head_digest=digest,
            lines_read=F("lines_read") + len(lines),
            lines_unparsed=F("lines_unparsed") + unparsed,
            last_read_at=timezone.now(),
        )

        if budget and read >= budget:
            logger.info("Stopped at %d lines; the rest waits for the next pass", read)
            break

    written = _persist(rollup)
    geo.forget()

    return {"files": len(files), "lines": read, "unparsed": unparsed, **written}


def _persist(rollup: _Rollup) -> dict:
    """
    Write the folded counters.

    Each row is read, added to and saved rather than written with an upsert
    expression, because the same day is touched by every pass and the counts
    have to accumulate across them.
    """
    with transaction.atomic():
        geo_totals: dict = defaultdict(lambda: [0, 0, 0, 0])

        for (day, domain, ip), counts in rollup.clients.items():
            row, created = ClientDay.objects.get_or_create(
                date=day,
                domain=domain,
                ip=ip,
                defaults={"last_seen": counts.last_seen or timezone.now()},
            )
            if created:
                # Resolved once per client per day, not per request.
                where = geo.locate(ip)
                row.country = where.country
                row.country_name = where.country_name
                row.city = where.city
                row.latitude = where.latitude
                row.longitude = where.longitude
                row.asn = where.asn
                row.asn_name = where.asn_name

            row.requests += counts.requests
            row.bytes_sent += counts.bytes_sent
            row.errors += counts.errors
            row.server_errors += counts.server_errors
            row.total_ms += counts.total_ms
            if counts.last_seen and (row.last_seen is None or counts.last_seen > row.last_seen):
                row.last_seen = counts.last_seen
            row.save()

            key = (day, domain, row.country, row.asn)
            totals = geo_totals[key]
            totals[0] += counts.requests
            totals[1] += counts.bytes_sent
            totals[2] += counts.errors
            totals[3] += 1 if created else 0
            geo_totals[key] = totals
            geo_totals[("names", key)] = (row.country_name, row.asn_name)

        for key, totals in list(geo_totals.items()):
            if key and key[0] == "names":
                continue
            day, domain, country, asn = key
            names = geo_totals.get(("names", key), ("", ""))
            row, _ = GeoDay.objects.get_or_create(
                date=day,
                domain=domain,
                country=country,
                asn=asn,
                defaults={"country_name": names[0], "asn_name": names[1]},
            )
            GeoDay.objects.filter(pk=row.pk).update(
                requests=F("requests") + totals[0],
                bytes_sent=F("bytes_sent") + totals[1],
                errors=F("errors") + totals[2],
                clients=F("clients") + totals[3],
            )

        for (day, domain, path), counts in rollup.paths.items():
            row, _ = PathDay.objects.get_or_create(date=day, domain=domain, path=path)
            PathDay.objects.filter(pk=row.pk).update(
                requests=F("requests") + counts.requests,
                bytes_sent=F("bytes_sent") + counts.bytes_sent,
                errors=F("errors") + counts.errors,
                server_errors=F("server_errors") + counts.server_errors,
                cache_hits=F("cache_hits") + counts.cache_hits,
                total_ms=F("total_ms") + counts.total_ms,
                max_ms=models_greatest(row.max_ms, counts.max_ms),
            )

        for (day, domain, kind), counts in rollup.agents.items():
            row, created = AgentDay.objects.get_or_create(
                date=day, domain=domain, kind=kind,
                defaults={"example": counts.example},
            )
            AgentDay.objects.filter(pk=row.pk).update(
                requests=F("requests") + counts.requests,
                bytes_sent=F("bytes_sent") + counts.bytes_sent,
            )

        for (day, hour, domain), counts in rollup.hours.items():
            row, _ = HourBucket.objects.get_or_create(date=day, hour=hour, domain=domain)
            HourBucket.objects.filter(pk=row.pk).update(
                requests=F("requests") + counts.requests,
                bytes_sent=F("bytes_sent") + counts.bytes_sent,
                errors=F("errors") + counts.errors,
                total_ms=F("total_ms") + counts.total_ms,
            )

    return {
        "clients": len(rollup.clients),
        "paths": len(rollup.paths),
        "hours": len(rollup.hours),
    }


def models_greatest(existing: float, candidate: float) -> float:
    """max() for a field being updated, without a database-specific function."""
    return max(existing or 0.0, candidate or 0.0)


def prune() -> dict:
    """
    Apply each table's retention.

    Addresses go first and soonest. The aggregates they fed stay, which is
    the point of writing both: the long view survives the deletion of the
    personal data it was built from.
    """
    today = timezone.now().date()

    clients, _ = ClientDay.objects.filter(
        date__lt=today - timedelta(days=settings.ANALYTICS_CLIENT_RETENTION_DAYS)
    ).delete()
    paths, _ = PathDay.objects.filter(
        date__lt=today - timedelta(days=settings.ANALYTICS_PATH_RETENTION_DAYS)
    ).delete()

    cutoff = today - timedelta(days=settings.ANALYTICS_AGGREGATE_RETENTION_DAYS)
    geo_rows, _ = GeoDay.objects.filter(date__lt=cutoff).delete()
    hours, _ = HourBucket.objects.filter(date__lt=cutoff).delete()
    agents, _ = AgentDay.objects.filter(date__lt=cutoff).delete()

    # A cursor for a file that no longer exists keeps an offset nothing will
    # ever use again.
    directory = Path(settings.NGINX_LOG_DIR)
    present = {path.name for path in log_files(directory)}
    cursors = 0
    if present or directory.is_dir():
        cursors, _ = LogCursor.objects.exclude(file_name__in=present).delete()

    return {
        "clients": clients,
        "paths": paths,
        "geo": geo_rows,
        "hours": hours,
        "agents": agents,
        "cursors": cursors,
    }
