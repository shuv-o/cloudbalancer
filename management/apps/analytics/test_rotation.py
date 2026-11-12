"""
Rotation, from the reading side.

The gateway rotates its own access logs on its own schedule — it holds the
only read-write mount of them, and leaving it to the control plane would
mean a disk that fills whenever the control plane is down, which has
happened. The consequence is that a file can be renamed out from under this
app's cursor at any moment, including between reading a file and writing
where it got to.

Two things can go wrong when that happens, and they fail in opposite
directions:

  * lines written to the old file and never read are lost, because the name
    they were under now holds a new empty file
  * lines already read are counted again, because the renamed file looks
    like one that has never been seen

Both are silent. The first looks like a traffic dip, the second like a
spike. These tests pin both, by rotating a file the way the watcher does and
asserting the totals.
"""
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from apps.analytics.models import ClientDay, LogCursor, PathDay
from apps.analytics.parser import domain_for, live_name, log_files
from apps.analytics.services import ingest
from apps.analytics.tests import line


class RotatingTestCase(TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cb-rotate-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        patch = override_settings(NGINX_LOG_DIR=str(self.directory))
        patch.enable()
        self.addCleanup(patch.disable)

    def write(self, domain, *lines):
        path = self.directory / f"{domain}.access.log"
        with path.open("a") as handle:
            for one in lines:
                handle.write(one + "\n")
        return path

    def rotate(self, domain):
        """
        What reload-watcher.sh does: rename, then a new file takes the name.

        Nginx creates the replacement on reopen; here the next write does.
        """
        live = self.directory / f"{domain}.access.log"
        live.rename(self.directory / f"{domain}.access.log.1")

    def requests_counted(self, domain="example.com"):
        return sum(
            row.requests for row in ClientDay.objects.filter(domain=domain)
        )


class NothingIsLostTests(RotatingTestCase):
    def test_lines_written_before_a_rotation_are_still_counted(self):
        """The plain case: everything is read after the rename rather than before."""
        self.write("example.com", line(), line(), line())
        self.rotate("example.com")

        ingest()

        self.assertEqual(self.requests_counted(), 3)

    def test_the_tail_of_a_half_read_file_survives_the_rename(self):
        """
        The case rotation exists to break. Three lines are read, two more are
        written, and the file is renamed before the next pass — so those two
        exist only in the rotated file.
        """
        self.write("example.com", line(), line(), line())
        ingest()
        self.assertEqual(self.requests_counted(), 3)

        self.write("example.com", line(), line())
        self.rotate("example.com")
        self.write("example.com", line())

        ingest()

        self.assertEqual(self.requests_counted(), 6)

    def test_a_rotation_with_no_reader_in_between_loses_nothing(self):
        """Two rotations' worth of traffic, read once at the end."""
        self.write("example.com", line())
        ingest()

        self.write("example.com", line(), line())
        self.rotate("example.com")
        self.write("example.com", line(), line())

        ingest()

        self.assertEqual(self.requests_counted(), 5)


class NothingIsCountedTwiceTests(RotatingTestCase):
    def test_a_rotated_file_is_not_re_read_from_the_start(self):
        """
        The defect this guards. Without the handover the renamed file has no
        cursor, is read from byte zero, and every line in it is counted a
        second time.
        """
        self.write("example.com", *[line() for _ in range(10)])
        ingest()
        self.assertEqual(self.requests_counted(), 10)

        self.rotate("example.com")
        self.write("example.com", line())

        ingest()

        self.assertEqual(self.requests_counted(), 11)

    def test_repeated_passes_over_a_rotated_file_add_nothing(self):
        """The handover must be idempotent; it runs on every pass."""
        self.write("example.com", *[line() for _ in range(4)])
        ingest()
        self.rotate("example.com")
        self.write("example.com", line())

        for _ in range(3):
            ingest()

        self.assertEqual(self.requests_counted(), 5)

    def test_the_cursor_is_handed_over_rather_than_duplicated(self):
        self.write("example.com", *[line() for _ in range(4)])
        ingest()
        before = LogCursor.objects.get(file_name="example.com.access.log").offset

        self.rotate("example.com")
        result = ingest()

        self.assertEqual(result["rotations_followed"], 1)
        rotated = LogCursor.objects.get(file_name="example.com.access.log.1")
        self.assertEqual(rotated.offset, before)

    def test_a_second_rotation_does_not_resurrect_the_first(self):
        """
        .1 is replaced by a different file. Its fingerprint changes, so the
        cursor following it is stale — and the new .1 is the file the live
        cursor was on, so it must inherit that offset, not start over.
        """
        self.write("example.com", line(), line())
        ingest()
        self.rotate("example.com")

        self.write("example.com", line(), line(), line())
        ingest()
        self.assertEqual(self.requests_counted(), 5)

        self.rotate("example.com")
        self.write("example.com", line())
        ingest()

        self.assertEqual(self.requests_counted(), 6)

    def test_an_unrotated_file_is_never_mistaken_for_one(self):
        """An old .1 sitting beside a live file that has not been renamed."""
        self.write("example.com", line(), line())
        self.rotate("example.com")
        self.write("example.com", line(), line(), line())
        ingest()
        self.assertEqual(self.requests_counted(), 5)

        self.write("example.com", line())
        ingest()

        self.assertEqual(self.requests_counted(), 6)


class ForeignRotationTests(RotatingTestCase):
    """
    A .1 that this app did not produce.

    This gateway ran a host logrotate with copytruncate until rotation moved
    into the application, and the .1 files it left are still on disk. Whether
    one of them has already been counted depends on when it was made:

      * just now -- the copy is byte-identical to what the cursor was
        reading, so the fingerprint matches and resuming at the same offset
        is exactly right
      * yesterday -- the live file has been truncated and rewritten since,
        the cursor has moved on, and nothing in the fingerprint relates the
        two any more. Those lines were read through the live file before the
        truncate, so reading them again would add a day of traffic that
        already happened.

    The second is the one on the server at the time rotation moved into the
    application, and it is the one these pin.
    """

    def copytruncate(self, domain):
        """logrotate's scheme: copy the whole file, then empty the original."""
        live = self.directory / f"{domain}.access.log"
        shutil.copy2(live, self.directory / f"{domain}.access.log.1")
        live.write_text("")

    def stale_rotation(self, domain, count):
        """
        A .1 left by yesterday's logrotate, unrelated to anything the cursor
        is holding now.
        """
        path = self.directory / f"{domain}.access.log.1"
        path.write_text(
            "".join(line(ip="192.0.2.5") + "\n" for _ in range(count))
        )
        return path

    def test_a_stale_rotation_is_skipped_rather_than_counted(self):
        self.write("example.com", *[line(ip="198.51.100.1") for _ in range(10)])
        ingest()
        self.assertEqual(self.requests_counted(), 10)

        self.stale_rotation("example.com", 7)

        result = ingest()

        self.assertEqual(result["rotations_skipped"], 1)
        self.assertEqual(result["rotations_followed"], 0)
        self.assertEqual(self.requests_counted(), 10)

    def test_a_fresh_copytruncate_resumes_rather_than_re_reading(self):
        """
        The other shape of the same thing. The copy still carries the bytes
        the cursor recorded, so it is recognisable — and resuming at the
        offset reads nothing, which is correct.
        """
        self.write("example.com", *[line() for _ in range(10)])
        ingest()

        self.copytruncate("example.com")
        self.write("example.com", line())

        result = ingest()

        self.assertEqual(result["rotations_followed"], 1)
        self.assertEqual(self.requests_counted(), 11)

    def test_history_on_a_fresh_install_is_read_rather_than_skipped(self):
        """
        Nothing has ever been read for this domain, so a .1 sitting there
        cannot be duplicating anything and its lines are real traffic.
        """
        self.write("example.com", line(), line())
        (self.directory / "example.com.access.log").rename(
            self.directory / "example.com.access.log.1"
        )
        self.write("example.com", line())

        result = ingest()

        self.assertEqual(result["rotations_skipped"], 0)
        self.assertEqual(self.requests_counted(), 3)

    def test_a_skipped_file_is_not_reconsidered_every_pass(self):
        self.write("example.com", *[line(ip="198.51.100.1") for _ in range(5)])
        ingest()
        self.stale_rotation("example.com", 4)

        first = ingest()
        second = ingest()
        third = ingest()

        self.assertEqual(first["rotations_skipped"], 1)
        self.assertEqual(second["rotations_skipped"], 0)
        self.assertEqual(third["rotations_skipped"], 0)
        self.assertEqual(self.requests_counted(), 5)

    def test_lines_appended_to_a_skipped_file_are_still_picked_up(self):
        """
        Skipping means "start at the end", not "ignore this file". Nothing
        appends to a rotated log in practice, but the cursor should behave
        like every other one.
        """
        self.write("example.com", *[line(ip="198.51.100.1") for _ in range(3)])
        ingest()
        self.stale_rotation("example.com", 2)
        ingest()
        self.assertEqual(self.requests_counted(), 3)

        with (self.directory / "example.com.access.log.1").open("a") as handle:
            handle.write(line(ip="198.51.100.9") + "\n")

        ingest()

        self.assertEqual(self.requests_counted(), 4)


class DiscoveryTests(RotatingTestCase):
    def test_a_rotated_file_belongs_to_the_same_domain(self):
        self.assertEqual(
            domain_for(Path("/var/log/nginx/a.example.com.access.log.1")),
            "a.example.com",
        )

    def test_the_live_name_is_recoverable(self):
        self.assertEqual(
            live_name("a.example.com.access.log.1"), "a.example.com.access.log"
        )

    def test_both_generations_are_offered_and_the_live_one_first(self):
        self.write("example.com", line())
        self.rotate("example.com")
        self.write("example.com", line())

        names = [path.name for path in log_files(self.directory)]

        self.assertEqual(
            names, ["example.com.access.log", "example.com.access.log.1"]
        )

    def test_compressed_history_is_left_alone(self):
        """
        .2 onward is for a human with zgrep. Everything in it was counted two
        rotations ago, and it is gzip rather than text besides.
        """
        self.write("example.com", line())
        (self.directory / "example.com.access.log.2.gz").write_bytes(b"\x1f\x8b junk")
        (self.directory / "example.com.access.log.3.gz").write_bytes(b"\x1f\x8b junk")

        names = [path.name for path in log_files(self.directory)]

        self.assertEqual(names, ["example.com.access.log"])

    def test_the_catch_all_log_is_skipped_in_both_generations(self):
        """
        access.log serves hostnames the gateway does not know. Attributing it
        to a domain called "access" was wrong before rotation and is still
        wrong after it.
        """
        (self.directory / "access.log").write_text(line() + "\n")
        (self.directory / "access.log.1").write_text(line() + "\n")
        self.write("example.com", line())

        names = [path.name for path in log_files(self.directory)]

        self.assertEqual(names, ["example.com.access.log"])


class AccountingTests(RotatingTestCase):
    def test_unparsed_lines_are_charged_to_the_file_they_were_in(self):
        """
        The count used to be the pass's running total rather than the file's,
        so every file after the first inherited the rejects of the ones
        before it and the column grew with the number of domains.
        """
        self.write("a.example.com", "not a log line at all")
        self.write("b.example.com", line())

        ingest()

        self.assertEqual(
            LogCursor.objects.get(file_name="a.example.com.access.log").lines_unparsed, 1
        )
        self.assertEqual(
            LogCursor.objects.get(file_name="b.example.com.access.log").lines_unparsed, 0
        )

    def test_paths_from_both_generations_land_on_one_row(self):
        self.write("example.com", line(request="GET /papers HTTP/1.1"))
        self.rotate("example.com")
        self.write("example.com", line(request="GET /papers HTTP/1.1"))

        ingest()

        row = PathDay.objects.get(domain="example.com", path="/papers")
        self.assertEqual(row.requests, 2)
