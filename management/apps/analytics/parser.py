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
import hashlib
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

# What the rotation in reload-watcher.sh renames the live file to. Only the
# first generation is read: .2 onward is compressed history, and by the time
# a file reaches it everything in it has been counted twice over.
ROTATED_SUFFIX = ".1"


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
    """
    The domain a log file belongs to, from its name.

    example.com.access.log and example.com.access.log.1 are the same domain:
    the second is only the first after a rename, and the requests in it were
    served by the same server block.
    """
    name = path.name
    if name.endswith(ROTATED_SUFFIX):
        name = name[: -len(ROTATED_SUFFIX)]
    if name.endswith(LOG_SUFFIX):
        return name[: -len(LOG_SUFFIX)]
    return name


def live_name(rotated: str) -> str:
    """The file a rotated one was, before the rename."""
    if rotated.endswith(ROTATED_SUFFIX):
        return rotated[: -len(ROTATED_SUFFIX)]
    return rotated


def log_files(directory: Path) -> list[Path]:
    """
    Every per-domain access log, live and once-rotated.

    access.log itself is skipped: it is the catch-all server's, which by
    definition serves requests for hostnames this gateway does not know, and
    attributing those to a domain called "access" would be a lie.

    The .1 files are included because rotation happens in the gateway on its
    own schedule, not on this one. Between two passes here, a file can be
    renamed out from under the cursor with lines in it that were never read;
    they are in .1 and nowhere else. Reading both is what makes rotation
    invisible to the counts rather than a small hole in them.

    Live files sort before their own rotation, which is what the caller
    wants: it reconciles the pair in that order.
    """
    if not directory.is_dir():
        return []

    skip = {f"access{LOG_SUFFIX}", f"access{LOG_SUFFIX}{ROTATED_SUFFIX}"}

    return sorted(
        path
        for pattern in (f"*{LOG_SUFFIX}", f"*{LOG_SUFFIX}{ROTATED_SUFFIX}")
        for path in directory.glob(pattern)
        if path.is_file() and path.name not in skip
    )


HEAD_BYTES = 256


def head_digest(path: Path) -> str:
    """
    A fingerprint of the first bytes of a file.

    Used to notice that a log has been replaced. The inode alone is not
    enough: a filesystem is free to hand the same inode to the next file
    created, and GitHub's runners do. When that happens to a log rotated
    while the gateway is busy, the new file is longer than the old offset,
    nothing looks wrong, and everything before that offset is skipped.

    The first line of an access log is a request with a timestamp, so two
    different files sharing these bytes is not a case worth designing for.
    """
    try:
        with path.open("rb") as handle:
            return hashlib.sha256(handle.read(HEAD_BYTES)).hexdigest()[:32]
    except OSError:
        return ""


def read_new_lines(
    path: Path, *, offset: int, inode: int | None, digest: str = ""
) -> tuple[list[str], int, int, str]:
    """
    The lines added since `offset`, and where to resume next time.

    Returns (lines, new_offset, inode, head_digest).

    Three things mean the file is not the one the offset refers to, and any
    of them restarts the read:

      * the first bytes changed -- it was replaced
      * the inode changed -- it was replaced, on a filesystem that says so
      * it is shorter than the offset -- it was truncated in place

    Resuming at a stale offset skips the beginning of the new file silently,
    and in proportion to how busy the gateway was.
    """
    try:
        stat = path.stat()
    except OSError:
        return [], offset, inode or 0, digest

    current_inode = stat.st_ino
    current_digest = head_digest(path)
    start = offset

    if digest and current_digest and current_digest != digest:
        logger.info("%s was replaced; reading it from the start", path.name)
        start = 0
    elif inode is not None and current_inode != inode:
        logger.info("%s was rotated; reading it from the start", path.name)
        start = 0
    elif stat.st_size < offset:
        logger.info("%s is shorter than where we left off; reading from the start", path.name)
        start = 0

    if stat.st_size == start:
        return [], start, current_inode, current_digest

    with path.open("r", errors="replace") as handle:
        handle.seek(start)
        body = handle.read()
        # Where the reader stops matters more than it looks: stopping mid-line
        # and resuming from there would split one request across two runs and
        # parse neither half. So the tail after the last newline is left for
        # next time.
        cut = body.rfind("\n")
        if cut == -1:
            return [], start, current_inode, current_digest

        consumed = body[: cut + 1]
        return (
            consumed.splitlines(),
            start + len(consumed.encode()),
            current_inode,
            current_digest,
        )
