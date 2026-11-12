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
from apps.analytics.parser import (
    ROTATED_SUFFIX,
    domain_for,
    head_digest,
    live_name,
    log_files,
    parse_line,
    read_new_lines,
)

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


def _hand_over_rotated_cursors(files: list[Path]) -> dict:
    """
    Decide what to do with each rotated file, before anything is read.

    The gateway rotates on its own schedule. When it does, the file this app
    was following is renamed to .1 and a new, empty one takes its name, so
    the next pass sees two files and neither of them is where it left off:

      * the live name has different content, so it is read from the start --
        correct, it is a different file
      * the .1 name has no cursor, so it would also be read from the start --
        wrong, most of it has already been counted

    Left alone, every rotation would re-count a whole file. The fix is to
    recognise the renamed file and give it the offset that was following it.
    The proof that it is the same file is the fingerprint: if .1 begins with
    the bytes the live cursor recorded, the live cursor was reading this file
    before the rename.

    That test is also what stops it happening twice. After the handover the
    live cursor holds the new file's fingerprint, which does not match .1 any
    more, so a second pass adopts nothing.

    When the fingerprint does not match, the question is whether .1 holds
    lines this app has never seen. There is no way to prove it either way, so
    it turns on whether this app has been reading that domain at all:

      * no cursor for the live file -- nothing has ever been read here, so
        .1 is genuinely unread history and is read from the start
      * a cursor exists, but .1 is not the file it was on -- .1 came from
        some other arrangement, and its lines were in all likelihood already
        counted through the live file. It is skipped to the end.

    That asymmetry is deliberate. This gateway ran a host logrotate with
    copytruncate before rotation moved into the application: .1 there is a
    copy from byte zero of a file that was read in place, so reading it would
    add a day of traffic that already happened. Under-counting a rotation in
    a rare case is recoverable and visible; silently doubling a day's traffic
    looks like growth and is never questioned.

    Returns a count of each decision, which the caller reports.
    """
    followed = skipped = 0

    for path in files:
        if not path.name.endswith(ROTATED_SUFFIX):
            continue

        try:
            stat = path.stat()
        except OSError:
            continue
        digest = head_digest(path)

        target = LogCursor.objects.filter(file_name=path.name).first()
        if (
            target is not None
            and target.head_digest == digest
            and target.inode == stat.st_ino
        ):
            # Already following this exact file. read_new_lines takes it from
            # here. Identity is the fingerprint and the inode together: a
            # second rotation puts a different file under the same name, and
            # consecutive logs often share their first bytes.
            continue

        source = LogCursor.objects.filter(file_name=live_name(path.name)).first()
        if source is None:
            # Nothing has ever been read for this domain, so there is nothing
            # this file could be duplicating. Read it from the start.
            continue

        # Is this the file the live cursor was on when it was renamed? Under
        # copytruncate the copy is byte-identical to what was being read, so
        # this matches there too, and resuming at the same offset is right in
        # both cases.
        same_file = (
            bool(source.head_digest)
            and digest == source.head_digest
            and stat.st_size >= source.offset
        )

        if target is None:
            target = LogCursor.objects.create(file_name=path.name)

        if same_file:
            LogCursor.objects.filter(pk=target.pk).update(
                offset=source.offset,
                # The rotated file's own inode, not the live one's. Under a
                # rename they are the same; under copytruncate .1 is a copy
                # with an inode of its own, and recording the live file's
                # would make the very next read decide the file had been
                # replaced and start it over.
                inode=stat.st_ino,
                head_digest=digest,
            )
            logger.info(
                "%s was rotated to %s; resuming at %d rather than re-reading it",
                source.file_name,
                path.name,
                source.offset,
            )
            followed += 1
        else:
            LogCursor.objects.filter(pk=target.pk).update(
                offset=stat.st_size,
                inode=stat.st_ino,
                head_digest=digest,
            )
            logger.info(
                "%s is not the file %s was being read from; skipping its %d bytes "
                "rather than risk counting them twice",
                path.name,
                source.file_name,
                stat.st_size,
            )
            skipped += 1

    return {"rotations_followed": followed, "rotations_skipped": skipped}


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

    # Before a byte is read. A rotation that happened since the last pass
    # has to be recognised first, or the renamed file looks new and gets
    # counted a second time.
    handovers = _hand_over_rotated_cursors(files)

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
        rejected = 0
        for line in lines:
            request = parse_line(line)
            if request is None:
                rejected += 1
                continue
            rollup.add(domain, request)

        read += len(lines)
        unparsed += rejected
        LogCursor.objects.filter(pk=cursor.pk).update(
            offset=offset,
            inode=inode,
            head_digest=digest,
            lines_read=F("lines_read") + len(lines),
            # This file's rejects, not the pass's. It used to add the running
            # total, so every file after the first inherited the ones before
            # it and the column grew quadratically in the number of domains.
            lines_unparsed=F("lines_unparsed") + rejected,
            last_read_at=timezone.now(),
        )

        if budget and read >= budget:
            logger.info("Stopped at %d lines; the rest waits for the next pass", read)
            break

    written = _persist(rollup)
    geo.forget()

    return {
        "files": len(files),
        "lines": read,
        "unparsed": unparsed,
        **handovers,
        **written,
    }


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
