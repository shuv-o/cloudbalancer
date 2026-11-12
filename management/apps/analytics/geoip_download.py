"""
Fetching the geolocation databases.

Not shipped in the image, for three reasons: they are licensed, they are
roughly 100 MB, and they go stale monthly. A copy baked into an image is
wrong within weeks and wrong in a way nobody notices, because a stale
database still answers -- just incorrectly, about the networks that have
changed hands since.

Two providers, because they ask different things of you:

    DB-IP     free, monthly, no account. CC-BY, so the attribution has to
              appear wherever the data does, which the panel shows.
    MaxMind   free, more accurate, needs an account and a licence key.

Both publish MaxMind-format files, so only the download differs and nothing
downstream knows which was used.
"""
import logging
import shutil
import tarfile
import tempfile
from datetime import date
from pathlib import Path

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

DBIP_BASE = "https://download.db-ip.com/free"
MAXMIND_BASE = "https://download.maxmind.com/app/geoip_download"

TIMEOUT = 300


class GeoipError(Exception):
    """A database could not be fetched."""


def _recent_months(count: int) -> list[date]:
    """This month and the ones before it, newest first."""
    today = date.today()
    year, month = today.year, today.month
    months = []
    for _ in range(count):
        months.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return months


def _dbip_url(kind: str, when: date) -> str:
    # DB-IP publishes one file per month, named for it. The current month may
    # not exist yet on the first of it, which is why the caller walks back.
    name = {"city": "dbip-city-lite", "asn": "dbip-asn-lite"}[kind]
    return f"{DBIP_BASE}/{name}-{when:%Y-%m}.mmdb.gz"


def _maxmind_url(kind: str) -> str:
    edition = {"city": "GeoLite2-City", "asn": "GeoLite2-ASN"}[kind]
    return (
        f"{MAXMIND_BASE}?edition_id={edition}"
        f"&license_key={settings.MAXMIND_LICENSE_KEY}&suffix=tar.gz"
    )


def _download(url: str, destination: Path) -> int:
    with httpx.stream("GET", url, timeout=TIMEOUT, follow_redirects=True) as response:
        if response.status_code == 404:
            raise FileNotFoundError(url)
        response.raise_for_status()
        written = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 256):
                handle.write(chunk)
                written += len(chunk)
    return written


def _extract_mmdb(archive: Path, scratch: Path) -> Path:
    """Pull the .mmdb out of whichever wrapper the provider used."""
    if archive.name.endswith(".gz") and not archive.name.endswith(".tar.gz"):
        import gzip

        target = scratch / "database.mmdb"
        with gzip.open(archive, "rb") as source, target.open("wb") as out:
            shutil.copyfileobj(source, out)
        return target

    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.name.endswith(".mmdb"):
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                target = scratch / "database.mmdb"
                with target.open("wb") as out:
                    shutil.copyfileobj(extracted, out)
                return target

    raise GeoipError(f"{archive.name} contains no .mmdb file")


def _install(source: Path, destination: Path) -> None:
    """
    Put the new file in place without a window where there is no file.

    A reader opens the path and maps it; replacing it by writing in place
    would have it reading a half-written database. Writing beside and
    renaming makes the swap atomic, and geo.py reopens when the mtime moves.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_suffix(".staging")
    shutil.copy2(source, staged)
    staged.replace(destination)


def update(kind: str) -> dict:
    """Fetch one database. Returns what happened, for the command to print."""
    destination = Path(
        settings.GEOIP_CITY_DB if kind == "city" else settings.GEOIP_ASN_DB
    )
    provider = settings.GEOIP_PROVIDER.lower()

    with tempfile.TemporaryDirectory(prefix="cb-geoip-") as scratch_name:
        scratch = Path(scratch_name)
        archive = scratch / "download"

        if provider == "maxmind":
            if not settings.MAXMIND_LICENSE_KEY:
                raise GeoipError(
                    "GEOIP_PROVIDER is maxmind but MAXMIND_LICENSE_KEY is not set. "
                    "Create a free account at maxmind.com, generate a licence key, "
                    "and put it in .env -- or set GEOIP_PROVIDER=dbip, which needs "
                    "no account."
                )
            size = _download(_maxmind_url(kind), archive)
            url = "maxmind"
        else:
            # On the first of a month the new file may not be published yet,
            # and failing then would leave an installation with no database
            # for a day. So the current month is tried, then the ones before.
            size = 0
            url = ""
            for when in _recent_months(3):
                candidate = _dbip_url(kind, when)
                try:
                    size = _download(candidate, archive)
                    url = candidate
                    break
                except FileNotFoundError:
                    logger.info("%s is not published yet; trying the month before", candidate)
            if not size:
                raise GeoipError(
                    f"No DB-IP {kind} database found for the last three months. "
                    "Check outbound access to download.db-ip.com."
                )

        mmdb = _extract_mmdb(archive, scratch)
        _install(mmdb, destination)

    return {
        "kind": kind,
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "downloaded_bytes": size,
        "source": url,
    }


def update_all() -> list[dict]:
    """
    Both databases, each reported separately.

    One failing does not stop the other: a city database without an ASN one
    still answers "which country", which is most of the value.
    """
    results = []
    for kind in ("city", "asn"):
        try:
            results.append(update(kind))
        except Exception as exc:
            logger.error("Could not update the %s database: %s", kind, exc)
            results.append({"kind": kind, "error": str(exc)[:400]})
    return results
