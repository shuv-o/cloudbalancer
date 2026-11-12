"""
Unpacking a geolocation database, whichever shape the provider ships it in.

Written after `geoip_update` failed on a healthy 60 MB download with "bad
checksum". The file was fine; the code chose between a bare gzip and a
gzipped tar by looking at the local filename, which is "download" and ends
with neither. Every DB-IP file was therefore handed to tarfile, which
decompressed it, found no tar structure, and reported an error that says
nothing about the real problem.

So the tests feed it both real shapes and one damaged one.
"""
import gzip
import io
import shutil
import tarfile
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from apps.analytics.geoip_download import GeoipError, _extract_mmdb, _install, _recent_months

# Not a real database -- the extractor never parses one, it only has to put
# the right bytes in the right place.
PAYLOAD = b"\xab\xcdMMDB-pretend-database" + bytes(range(256)) * 4


class ExtractTests(TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="cb-geoip-test-"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)

    def bare_gzip(self) -> Path:
        """How DB-IP ships it: the database, gzipped."""
        path = self.scratch / "download"
        with gzip.open(path, "wb") as handle:
            handle.write(PAYLOAD)
        return path

    def gzipped_tar(self) -> Path:
        """How MaxMind ships it: a tar containing the database, gzipped."""
        path = self.scratch / "download"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("GeoLite2-City_20261101/GeoLite2-City.mmdb")
            info.size = len(PAYLOAD)
            tar.addfile(info, io.BytesIO(PAYLOAD))
        return path

    def test_a_bare_gzipped_database(self):
        """The case that failed in production."""
        extracted = _extract_mmdb(self.bare_gzip(), self.scratch)
        self.assertEqual(extracted.read_bytes(), PAYLOAD)

    def test_a_gzipped_tar_containing_one(self):
        extracted = _extract_mmdb(self.gzipped_tar(), self.scratch)
        self.assertEqual(extracted.read_bytes(), PAYLOAD)

    def test_the_filename_is_never_consulted(self):
        """
        The defect, pinned. Both shapes arrive under the same meaningless
        name, so anything deciding by extension gets one of them wrong.
        """
        for build in (self.bare_gzip, self.gzipped_tar):
            with self.subTest(shape=build.__name__):
                archive = build()
                self.assertEqual(archive.name, "download")
                self.assertEqual(_extract_mmdb(archive, self.scratch).read_bytes(), PAYLOAD)

    def test_a_short_database_is_not_mistaken_for_a_tar(self):
        """
        The tar magic sits at offset 257. A file shorter than that must not
        read as a truncated header and fall into the tar branch.
        """
        path = self.scratch / "download"
        with gzip.open(path, "wb") as handle:
            handle.write(b"tiny")

        self.assertEqual(_extract_mmdb(path, self.scratch).read_bytes(), b"tiny")

    def test_a_tar_with_no_database_says_so(self):
        path = self.scratch / "download"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("COPYRIGHT.txt")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"text"))

        with self.assertRaises(GeoipError) as caught:
            _extract_mmdb(path, self.scratch)

        self.assertIn("no .mmdb", str(caught.exception))

    def test_something_that_is_not_gzip_at_all_raises(self):
        """An HTML error page served with a 200, which providers do."""
        path = self.scratch / "download"
        path.write_bytes(b"<html>Not found</html>")

        with self.assertRaises(Exception):
            _extract_mmdb(path, self.scratch)


class InstallTests(TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="cb-geoip-install-"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)

    def test_it_replaces_an_existing_database(self):
        source = self.scratch / "new.mmdb"
        source.write_bytes(b"new")
        destination = self.scratch / "live" / "city.mmdb"
        destination.parent.mkdir()
        destination.write_bytes(b"old")

        _install(source, destination)

        self.assertEqual(destination.read_bytes(), b"new")

    def test_nothing_is_left_staged(self):
        """
        A reader opens this path and maps it. Writing in place would have it
        reading a half-written database, so the new file is written beside
        and renamed -- and the staging file must not survive.
        """
        source = self.scratch / "new.mmdb"
        source.write_bytes(b"new")
        destination = self.scratch / "city.mmdb"

        _install(source, destination)

        self.assertEqual(list(self.scratch.glob("*.staging")), [])

    def test_it_creates_the_directory_on_a_first_run(self):
        source = self.scratch / "new.mmdb"
        source.write_bytes(b"new")
        destination = self.scratch / "never" / "existed" / "city.mmdb"

        _install(source, destination)

        self.assertTrue(destination.exists())


class MonthTests(TestCase):
    def test_it_offers_this_month_and_the_ones_before(self):
        """
        On the first of a month the new file may not be published, and
        failing then would leave an installation with no database for a day.
        """
        months = _recent_months(3)

        self.assertEqual(len(months), 3)
        self.assertTrue(all(month.day == 1 for month in months))
        self.assertGreater(months[0], months[1])
        self.assertGreater(months[1], months[2])

    def test_it_crosses_a_year_boundary(self):
        months = _recent_months(14)
        self.assertEqual(len(set(months)), 14)


class GeoLookupTests(TestCase):
    """Behaviour with no database, which is every installation until it runs."""

    @override_settings(
        GEOIP_CITY_DB="/nonexistent/city.mmdb", GEOIP_ASN_DB="/nonexistent/asn.mmdb"
    )
    def test_lookups_return_empty_rather_than_failing(self):
        from apps.analytics import geo

        geo.forget()
        where = geo.locate("8.8.8.8")

        self.assertEqual(where.country, "")
        self.assertIsNone(where.asn)
        self.assertFalse(where.is_known)

    @override_settings(
        GEOIP_CITY_DB="/nonexistent/city.mmdb", GEOIP_ASN_DB="/nonexistent/asn.mmdb"
    )
    def test_the_panel_is_told_the_databases_are_missing(self):
        from apps.analytics import geo

        status = geo.available()

        self.assertFalse(status["any"])
        self.assertFalse(status["databases"]["city"]["present"])
        self.assertIn("db-ip", status["attribution"].lower())
