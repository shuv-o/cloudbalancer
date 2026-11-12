"""
Turning an address into a place and a network.

Two databases, because they answer different questions and only together
answer the useful one. A city database says traffic came from Dhaka; an ASN
database says it came from AS63969, which is the member institution actually
running the client. For a network operator the second is usually the one that
matters, and neither alone gives both.

Both are looked up in-process against local MaxMind-format files. The
alternative -- an Nginx GeoIP module writing country into the log -- would put
this in the data plane, where a bad database file or a module upgrade becomes
a gateway that will not start. Nothing here can take traffic down: the worst
case is an address that resolves to nothing, and the row is written anyway.

The files are not shipped. They are licensed, they go stale, and a copy
embedded in an image is wrong within a month. `manage.py geoip_update`
fetches them; until it has, every lookup returns empty and the rest of this
app works without geography.
"""
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Location:
    country: str = ""
    country_name: str = ""
    city: str = ""
    latitude: float | None = None
    longitude: float | None = None
    asn: int | None = None
    asn_name: str = ""

    @property
    def is_known(self) -> bool:
        return bool(self.country or self.asn)


EMPTY = Location()


class _Readers:
    """
    The open database handles, and whether they are current.

    Held open because a reader maps the file once and answers from memory;
    reopening per lookup would make a million-line aggregation pass a million
    file opens. Reopened when the file on disk changes, so `geoip_update`
    takes effect without restarting anything.
    """

    def __init__(self):
        self.city = None
        self.asn = None
        self._stamps: dict[str, float] = {}

    def _stamp(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _open(self, kind: str, path: Path):
        try:
            import geoip2.database
        except ImportError:  # pragma: no cover - the package is a dependency
            logger.warning("geoip2 is not installed; geolocation is unavailable")
            return None

        if not path.exists():
            return None

        try:
            return geoip2.database.Reader(str(path))
        except Exception as exc:
            logger.error("Could not open the %s database at %s: %s", kind, path, exc)
            return None

    def refresh(self) -> None:
        for kind, setting in (("city", "GEOIP_CITY_DB"), ("asn", "GEOIP_ASN_DB")):
            path = Path(getattr(settings, setting, "") or "")
            stamp = self._stamp(path)
            if stamp != self._stamps.get(kind):
                existing = getattr(self, kind)
                if existing is not None:
                    try:
                        existing.close()
                    except Exception:
                        pass
                setattr(self, kind, self._open(kind, path) if stamp else None)
                self._stamps[kind] = stamp


_readers = _Readers()


def available() -> dict:
    """What the panel shows about whether geolocation can work at all."""
    _readers.refresh()

    details = {}
    for kind, setting in (("city", "GEOIP_CITY_DB"), ("asn", "GEOIP_ASN_DB")):
        path = Path(getattr(settings, setting, "") or "")
        exists = path.exists()
        details[kind] = {
            "path": str(path),
            "present": exists,
            "bytes": path.stat().st_size if exists else 0,
            "updated_at": path.stat().st_mtime if exists else None,
            "loaded": getattr(_readers, kind) is not None,
        }

    return {
        "databases": details,
        "any": any(d["loaded"] for d in details.values()),
        "attribution": settings.GEOIP_ATTRIBUTION,
    }


@lru_cache(maxsize=50_000)
def _lookup(ip: str) -> Location:
    """
    One address.

    Cached because an aggregation pass sees the same client thousands of
    times in a run, and the cache is cleared between runs so a refreshed
    database is not shadowed by yesterday's answers.
    """
    city = country = country_name = ""
    latitude = longitude = None
    asn = None
    asn_name = ""

    if _readers.city is not None:
        try:
            response = _readers.city.city(ip)
            country = response.country.iso_code or ""
            country_name = response.country.name or ""
            city = response.city.name or ""
            latitude = response.location.latitude
            longitude = response.location.longitude
        except Exception:
            # A private address, or one the database has never heard of.
            # Both are ordinary and neither is worth a log line per request.
            pass

    if _readers.asn is not None:
        try:
            response = _readers.asn.asn(ip)
            asn = response.autonomous_system_number
            asn_name = (response.autonomous_system_organization or "")[:160]
        except Exception:
            pass

    return Location(
        country=country,
        country_name=country_name,
        city=city,
        latitude=latitude,
        longitude=longitude,
        asn=asn,
        asn_name=asn_name,
    )


def locate(ip: str) -> Location:
    """Where this address is, as far as the databases know."""
    _readers.refresh()
    if _readers.city is None and _readers.asn is None:
        return EMPTY
    return _lookup(ip)


def forget() -> None:
    """Drop the per-run cache. Called when a pass finishes."""
    _lookup.cache_clear()
