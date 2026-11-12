"""
Reading Nginx's access logs without reading them twice.

The traffic-status module cannot answer any of the questions this app exists
for. It aggregates per vhost and per upstream, which means it knows how many
requests a domain served and nothing at all about who made them, what they
asked for, or where they came from. Those facts are only in the access log.

Two things make that log usable here rather than merely present:

Each domain writes its own file -- 20-servers.conf.j2 emits
`access_log /var/log/nginx/<domain>.access.log` -- so the domain a request
belongs to comes from the filename and the log format needs no change.

And a cursor per file means each run reads only what is new. An aggregation
pass that re-read from the beginning would double every count it had already
recorded, which is the kind of error that looks like traffic growth.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# The `main` format from gateway/nginx/nginx.conf:
#
#   $remote_addr - $remote_user [$time_local] "$request"
#   $status $body_bytes_sent "$http_referer" "$http_user_agent"
#   rid=$request_id cache=$upstream_cache_status
#   rt=$request_time uct=$upstream_connect_time
#   uht=$upstream_header_time urt=$upstream_response_time
#   upstream=$upstream_addr
#
# Quoted fields are matched with [^"] rather than something cleverer because
# Nginx's default escaping turns a quote inside a header into \x22 before it
# is written. A user agent therefore cannot contain a raw quote, and the
# simple form is correct as well as fast.
# A timing field, which is not always one number.
#
# When proxy_next_upstream retries -- which this gateway is configured to do
# -- Nginx writes one value per attempt, separated by ", ". An internal
# redirect separates groups with " : ". A pattern of \S+ matches neither, so
# every retried request produced a line this parser rejected and dropped. The
# requests that failed over are exactly the ones worth seeing.
_TIMING = r"[\d.\-]+(?:\s*[,:]\s*[\d.\-]+)*"

LINE = re.compile(
    r'^(?P<ip>\S+) - (?P<user>\S+) \[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" '
    r'(?P<status>\d{3}) (?P<bytes>\d+) "(?P<referer>[^"]*)" "(?P<agent>[^"]*)" '
    r"rid=(?P<rid>\S+) cache=(?P<cache>\S+) "
    r"rt=(?P<rt>" + _TIMING + r") uct=(?P<uct>" + _TIMING + r") "
    r"uht=(?P<uht>" + _TIMING + r") urt=(?P<urt>" + _TIMING + r") "
    r"upstream=(?P<upstream>.*?)\s*$"
)

TIME_FORMAT = "%d/%b/%Y:%H:%M:%S %z"

# Suffix the config appends to each domain's log. The panel writes
# panel.access.log through the same mechanism, and that is traffic too.
LOG_SUFFIX = ".access.log"


@dataclass(frozen=True)
class Request:
    """One line, parsed. Only the fields anything here actually uses."""

    ip: str
    at: datetime
    method: str
    path: str
    status: int
    bytes_sent: int
    agent: str
    cache: str
    duration_ms: float

    @property
    def is_error(self) -> bool:
        return self.status >= 400

    @property
    def is_server_error(self) -> bool:
        return self.status >= 500


def _number(raw: str) -> float:
    """
    A timing field, or zero.

    Nginx writes "-" when a request never reached an upstream, and writes
    several comma-separated values when it tried more than one. Neither is a
    float, and both are ordinary.
    """
    if not raw or raw == "-":
        return 0.0
    first = re.split(r"[,:]", raw)[0].strip()
    try:
        return float(first)
    except ValueError:
        return 0.0


def parse_line(line: str) -> Request | None:
    """One log line, or None if it is not one."""
    match = LINE.match(line)
    if match is None:
        return None

    fields = match.groupdict()

    try:
        at = datetime.strptime(fields["time"], TIME_FORMAT)
    except ValueError:
        return None

    # "GET /path HTTP/1.1", or garbage from a client that sent garbage.
    parts = fields["request"].split(" ")
    method = parts[0][:10] if parts and parts[0] else ""
    path = parts[1] if len(parts) > 1 else ""

    return Request(
        ip=fields["ip"],
        at=at,
        method=method,
        path=path,
        status=int(fields["status"]),
        bytes_sent=int(fields["bytes"]),
        agent=fields["agent"],
        cache=fields["cache"] if fields["cache"] != "-" else "",
        duration_ms=_number(fields["rt"]) * 1000,
    )


def domain_for(path: Path) -> str:
    """The domain a log file belongs to, from its name."""
    name = path.name
    if name.endswith(LOG_SUFFIX):
        return name[: -len(LOG_SUFFIX)]
    return name


def log_files(directory: Path) -> list[Path]:
    """
    Every per-domain access log, newest-named first.

    access.log itself is skipped: it is the catch-all server's, which by
    definition serves requests for hostnames this gateway does not know, and
    attributing those to a domain called "access" would be a lie.
    """
    if not directory.is_dir():
        return []

    return sorted(
        path
        for path in directory.glob(f"*{LOG_SUFFIX}")
        if path.is_file() and path.name != f"access{LOG_SUFFIX}"
    )


def read_new_lines(path: Path, *, offset: int, inode: int | None) -> tuple[list[str], int, int]:
    """
    The lines added since `offset`, and where to resume next time.

    Returns (lines, new_offset, inode).

    The inode is checked because a rotated log is a different file wearing the
    same name. Resuming at the old offset would then skip the beginning of the
    new file -- silently, and in proportion to how busy the gateway was. When
    the inode changes, or the file is shorter than the offset, reading starts
    again from zero.
    """
    try:
        stat = path.stat()
    except OSError:
        return [], offset, inode or 0

    current_inode = stat.st_ino
    start = offset

    if inode is not None and current_inode != inode:
        logger.info("%s was rotated; reading it from the start", path.name)
        start = 0
    elif stat.st_size < offset:
        logger.info("%s is shorter than where we left off; reading from the start", path.name)
        start = 0

    if stat.st_size == start:
        return [], start, current_inode

    with path.open("r", errors="replace") as handle:
        handle.seek(start)
        body = handle.read()
        # Where the reader stops matters more than it looks: stopping mid-line
        # and resuming from there would split one request across two runs and
        # parse neither half. So the tail after the last newline is left for
        # next time.
        cut = body.rfind("\n")
        if cut == -1:
            return [], start, current_inode

        consumed = body[: cut + 1]
        return consumed.splitlines(), start + len(consumed.encode()), current_inode
