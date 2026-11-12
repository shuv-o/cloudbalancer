"""
Reading access logs into daily rollups.

The failure this is written against is not a crash. It is a pass that counts
the same request twice, or loses a day of them, and reports a number that
looks like traffic. Most of what follows is about the cursor: where reading
stopped, and whether resuming from there is still correct.
"""
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.analytics.models import (
    AgentDay,
    ClientDay,
    GeoDay,
    HourBucket,
    LogCursor,
    PathDay,
)
from apps.analytics.parser import domain_for, log_files, parse_line, read_new_lines
from apps.analytics.services import classify_agent, ingest, normalise_path, prune

AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0"


def line(
    ip="203.0.113.7",
    when="12/Nov/2026:10:15:00 +0000",
    request="GET /research/papers HTTP/1.1",
    status=200,
    size=4321,
    agent=AGENT,
    cache="HIT",
    rt="0.042",
) -> str:
    return (
        f'{ip} - - [{when}] "{request}" {status} {size} '
        f'"https://example.org/" "{agent}" '
        f"rid=abc123 cache={cache} rt={rt} uct=0.001 uht=0.010 urt=0.040 "
        f"upstream=10.0.0.1:8080"
    )


class ParseLineTests(TestCase):
    def test_a_normal_line(self):
        request = parse_line(line())

        self.assertEqual(request.ip, "203.0.113.7")
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, "/research/papers")
        self.assertEqual(request.status, 200)
        self.assertEqual(request.bytes_sent, 4321)
        self.assertEqual(request.cache, "HIT")
        self.assertAlmostEqual(request.duration_ms, 42.0)
        self.assertFalse(request.is_error)

    def test_an_ipv6_client(self):
        request = parse_line(line(ip="2001:db8::1"))
        self.assertEqual(request.ip, "2001:db8::1")

    def test_a_request_that_never_reached_an_upstream(self):
        """
        Nginx writes "-" for timings that did not happen. A float() on that
        raises, and one malformed field would otherwise discard the line.
        """
        request = parse_line(
            line().replace("rt=0.042", "rt=-").replace("urt=0.040", "urt=-")
        )
        self.assertIsNotNone(request)
        self.assertEqual(request.duration_ms, 0.0)

    def test_a_request_that_failed_over_to_a_second_upstream(self):
        """
        proxy_next_upstream is configured on this gateway, so this happens.
        Nginx writes one timing per attempt, separated by ", ", and one
        upstream address per attempt.

        The first version of this parser matched timings with \\S+, which
        matches neither -- so every retried request was dropped. Those are
        exactly the requests worth seeing.
        """
        retried = (
            line()
            .replace("rt=0.042", "rt=3.042")
            .replace("uct=0.001", "uct=3.000, 0.001")
            .replace("uht=0.010", "uht=-, 0.010")
            .replace("urt=0.040", "urt=-, 0.040")
            .replace("upstream=10.0.0.1:8080", "upstream=10.0.0.1:8080, 10.0.0.2:8080")
        )

        request = parse_line(retried)

        self.assertIsNotNone(request, "a failed-over request must still be counted")
        self.assertAlmostEqual(request.duration_ms, 3042.0)

    def test_an_internally_redirected_request(self):
        """Nginx separates groups with " : " for those."""
        request = parse_line(line().replace("urt=0.040", "urt=0.040 : 0.020"))
        self.assertIsNotNone(request)

    def test_a_request_line_that_is_not_one(self):
        """Anything may be sent to a public port, and most of it is not HTTP."""
        request = parse_line(line(request="\\x16\\x03\\x01"))
        self.assertIsNotNone(request)
        self.assertEqual(request.path, "")

    def test_errors_are_recognised(self):
        self.assertTrue(parse_line(line(status=404)).is_error)
        self.assertFalse(parse_line(line(status=404)).is_server_error)
        self.assertTrue(parse_line(line(status=502)).is_server_error)

    def test_a_line_from_another_format_is_skipped_not_guessed_at(self):
        self.assertIsNone(parse_line("this is not an access log line"))
        self.assertIsNone(parse_line(""))

    def test_the_domain_comes_from_the_filename(self):
        self.assertEqual(
            domain_for(Path("/var/log/nginx/sms.bdren.net.bd.access.log")),
            "sms.bdren.net.bd",
        )


class CursorTests(TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cb-logs-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.log = self.directory / "example.com.access.log"

    def write(self, *lines) -> None:
        with self.log.open("a") as handle:
            for one in lines:
                handle.write(one + "\n")

    def test_it_reads_everything_the_first_time(self):
        self.write(line(), line())
        lines, offset, inode = read_new_lines(self.log, offset=0, inode=None)

        self.assertEqual(len(lines), 2)
        self.assertEqual(offset, self.log.stat().st_size)

    def test_it_reads_only_what_is_new(self):
        """The whole point. Re-reading doubles every count already recorded."""
        self.write(line())
        _, offset, inode = read_new_lines(self.log, offset=0, inode=None)

        self.write(line(), line())
        lines, _, _ = read_new_lines(self.log, offset=offset, inode=inode)

        self.assertEqual(len(lines), 2)

    def test_a_half_written_line_waits_for_the_rest(self):
        """
        Nginx buffers and flushes; a read can land mid-line. Consuming the
        fragment would parse neither half and lose the request.
        """
        self.write(line())
        with self.log.open("a") as handle:
            handle.write("203.0.113.9 - - [12/Nov/2026:10:16:00 +0000] \"GET /part")

        lines, offset, _ = read_new_lines(self.log, offset=0, inode=None)

        self.assertEqual(len(lines), 1)
        self.assertLess(offset, self.log.stat().st_size)

        # And when the rest arrives, the whole line is read.
        with self.log.open("a") as handle:
            handle.write(' HTTP/1.1" 200 10 "-" "x" rid=a cache=- rt=0.1 '
                         "uct=0.1 uht=0.1 urt=0.1 upstream=-\n")
        more, _, _ = read_new_lines(self.log, offset=offset, inode=None)
        self.assertEqual(len(more), 1)

    def test_a_rotated_file_is_read_from_the_start(self):
        """
        A rotated log is a different file wearing the same name. Resuming at
        the old offset skips its beginning -- silently, and in proportion to
        how busy the gateway was.
        """
        self.write(line())
        _, offset, inode = read_new_lines(self.log, offset=0, inode=None)

        self.log.unlink()
        self.write(line(), line(), line())
        new_inode = self.log.stat().st_ino
        self.assertNotEqual(new_inode, inode)

        lines, _, _ = read_new_lines(self.log, offset=offset, inode=inode)
        self.assertEqual(len(lines), 3)

    def test_a_truncated_file_is_read_from_the_start(self):
        """copytruncate rotation leaves the same inode and a shorter file."""
        self.write(line(), line(), line())
        _, offset, inode = read_new_lines(self.log, offset=0, inode=None)

        self.log.write_text("")
        self.write(line())

        lines, _, _ = read_new_lines(self.log, offset=offset, inode=inode)
        self.assertEqual(len(lines), 1)

    def test_nothing_new_reads_nothing(self):
        self.write(line())
        _, offset, inode = read_new_lines(self.log, offset=0, inode=None)
        lines, same, _ = read_new_lines(self.log, offset=offset, inode=inode)

        self.assertEqual(lines, [])
        self.assertEqual(same, offset)

    def test_the_catch_all_log_is_not_treated_as_a_domain(self):
        """
        access.log belongs to the default server, which by definition serves
        hostnames this gateway does not know. Counting those under a domain
        called "access" would be a fiction.
        """
        (self.directory / "access.log").write_text(line() + "\n")
        names = [p.name for p in log_files(self.directory)]

        self.assertNotIn("access.log", names)


class NormalisationTests(TestCase):
    def test_a_query_string_is_dropped(self):
        self.assertEqual(normalise_path("/search?q=anything"), "/search")

    def test_numeric_segments_collapse(self):
        """
        Otherwise /users/1 .. /users/9999 is ten thousand rows hiding the one
        fact worth having.
        """
        self.assertEqual(normalise_path("/users/4821/papers"), "/users/{n}/papers")

    def test_identifier_segments_collapse(self):
        self.assertEqual(
            normalise_path("/files/0f8fad5bd9cb469fa16570867728950e/download"),
            "/files/{id}/download",
        )

    def test_an_ordinary_path_is_left_alone(self):
        self.assertEqual(normalise_path("/research/papers"), "/research/papers")

    def test_an_empty_path_is_the_root(self):
        self.assertEqual(normalise_path(""), "/")

    def test_a_very_long_path_is_bounded(self):
        self.assertLessEqual(len(normalise_path("/" + "a" * 900)), 512)


class AgentClassificationTests(TestCase):
    def test_a_browser(self):
        self.assertEqual(classify_agent(AGENT), AgentDay.Kind.BROWSER)

    def test_a_crawler_claiming_to_be_a_browser(self):
        """Most of them do, which is why the bot patterns are tried first."""
        self.assertEqual(
            classify_agent("Mozilla/5.0 (compatible; Googlebot/2.1)"),
            AgentDay.Kind.BOT,
        )

    def test_a_tool(self):
        self.assertEqual(classify_agent("curl/8.4.0"), AgentDay.Kind.TOOL)
        self.assertEqual(classify_agent("python-requests/2.31"), AgentDay.Kind.TOOL)

    def test_nothing_at_all(self):
        self.assertEqual(classify_agent("-"), AgentDay.Kind.UNKNOWN)
        self.assertEqual(classify_agent(""), AgentDay.Kind.UNKNOWN)


class IngestTests(TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cb-ingest-"))
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

    def test_it_records_a_client_against_the_right_domain(self):
        self.write("example.com", line())

        ingest()

        row = ClientDay.objects.get()
        self.assertEqual(row.domain, "example.com")
        self.assertEqual(row.ip, "203.0.113.7")
        self.assertEqual(row.requests, 1)
        self.assertEqual(row.bytes_sent, 4321)

    def test_two_domains_stay_separate(self):
        self.write("a.example.com", line())
        self.write("b.example.com", line())

        ingest()

        self.assertEqual(
            sorted(ClientDay.objects.values_list("domain", flat=True)),
            ["a.example.com", "b.example.com"],
        )

    def test_running_twice_does_not_double_the_counts(self):
        """
        The cursor's whole purpose. Re-reading would add every request again
        and look like the traffic grew.
        """
        self.write("example.com", line(), line())

        ingest()
        ingest()

        self.assertEqual(ClientDay.objects.get().requests, 2)

    def test_new_lines_accumulate_onto_the_same_day(self):
        self.write("example.com", line())
        ingest()

        self.write("example.com", line(), line())
        ingest()

        self.assertEqual(ClientDay.objects.get().requests, 3)

    def test_errors_are_counted_separately(self):
        self.write(
            "example.com",
            line(status=200), line(status=404), line(status=502),
        )

        ingest()

        row = ClientDay.objects.get()
        self.assertEqual(row.requests, 3)
        self.assertEqual(row.errors, 2)
        self.assertEqual(row.server_errors, 1)

    def test_paths_are_rolled_up_and_normalised(self):
        self.write(
            "example.com",
            line(request="GET /users/1 HTTP/1.1"),
            line(request="GET /users/2 HTTP/1.1"),
            line(request="GET /users/3?tab=x HTTP/1.1"),
        )

        ingest()

        row = PathDay.objects.get()
        self.assertEqual(row.path, "/users/{n}")
        self.assertEqual(row.requests, 3)

    def test_cache_hits_are_counted(self):
        self.write(
            "example.com",
            line(cache="HIT"), line(cache="MISS"), line(cache="HIT"),
        )

        ingest()

        row = PathDay.objects.get()
        self.assertEqual(row.cache_hits, 2)
        self.assertAlmostEqual(row.cache_hit_ratio, 200 / 3)

    def test_hours_are_bucketed_for_the_heatmap(self):
        self.write(
            "example.com",
            line(when="12/Nov/2026:09:00:00 +0000"),
            line(when="12/Nov/2026:09:30:00 +0000"),
            line(when="12/Nov/2026:14:00:00 +0000"),
        )

        ingest()

        buckets = {b.hour: b.requests for b in HourBucket.objects.all()}
        self.assertEqual(buckets, {9: 2, 14: 1})

    def test_agents_are_classified(self):
        self.write(
            "example.com",
            line(agent=AGENT),
            line(agent="Mozilla/5.0 (compatible; Googlebot/2.1)"),
            line(agent="curl/8.4.0"),
        )

        ingest()

        kinds = dict(AgentDay.objects.values_list("kind", "requests"))
        self.assertEqual(kinds[AgentDay.Kind.BROWSER], 1)
        self.assertEqual(kinds[AgentDay.Kind.BOT], 1)
        self.assertEqual(kinds[AgentDay.Kind.TOOL], 1)

    def test_a_geo_row_is_written_beside_every_client(self):
        """
        Written at the same time, not derived later, so the long view
        survives the deletion of the addresses it came from.
        """
        self.write("example.com", line())

        ingest()

        self.assertEqual(GeoDay.objects.count(), 1)
        self.assertEqual(GeoDay.objects.get().requests, 1)

    def test_unparsable_lines_are_counted_rather_than_fatal(self):
        self.write("example.com", line(), "garbage", line())

        result = ingest()

        self.assertEqual(result["unparsed"], 1)
        self.assertEqual(ClientDay.objects.get().requests, 2)

    def test_no_logs_is_reported_not_silent(self):
        """A pass that read nothing should be distinguishable from one that failed."""
        result = ingest()
        self.assertIn("no per-domain access logs", result["reason"])

    def test_a_cursor_is_kept_per_file(self):
        self.write("a.example.com", line())
        self.write("b.example.com", line())

        ingest()

        self.assertEqual(LogCursor.objects.count(), 2)
        for cursor in LogCursor.objects.all():
            self.assertGreater(cursor.offset, 0)

    @override_settings(ANALYTICS_MAX_LINES_PER_RUN=2)
    def test_a_backlog_is_worked_through_over_several_passes(self):
        """
        One pass must not try to swallow a week of logs: it would hold a
        worker for minutes and allocate the lot.
        """
        self.write("example.com", *[line() for _ in range(5)])

        first = ingest()
        self.assertEqual(first["lines"], 2)

        second = ingest()
        self.assertEqual(second["lines"], 2)

        ingest()
        self.assertEqual(ClientDay.objects.get().requests, 5)


class RetentionTests(TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="cb-retain-"))
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        patch = override_settings(NGINX_LOG_DIR=str(self.directory))
        patch.enable()
        self.addCleanup(patch.disable)

    def aged(self, days: int):
        when = timezone.now().date() - timedelta(days=days)
        ClientDay.objects.create(
            date=when, domain="example.com", ip="203.0.113.7",
            requests=10, last_seen=timezone.now(),
        )
        GeoDay.objects.create(date=when, domain="example.com", country="BD", requests=10)
        PathDay.objects.create(date=when, domain="example.com", path="/", requests=10)
        HourBucket.objects.create(date=when, hour=10, domain="example.com", requests=10)

    def test_addresses_are_deleted_well_before_the_aggregates(self):
        """
        The reason both tables exist. The address goes at thirty days; the
        country and network it fed stay for the full window.
        """
        self.aged(45)

        prune()

        self.assertEqual(ClientDay.objects.count(), 0)
        self.assertEqual(GeoDay.objects.count(), 1)
        self.assertEqual(HourBucket.objects.count(), 1)

    def test_recent_addresses_are_kept(self):
        self.aged(5)

        prune()

        self.assertEqual(ClientDay.objects.count(), 1)

    def test_paths_go_at_ninety_days(self):
        self.aged(120)

        prune()

        self.assertEqual(PathDay.objects.count(), 0)
        self.assertEqual(GeoDay.objects.count(), 1)

    def test_everything_goes_past_the_aggregate_window(self):
        self.aged(500)

        prune()

        self.assertEqual(GeoDay.objects.count(), 0)
        self.assertEqual(HourBucket.objects.count(), 0)

    def test_a_cursor_for_a_vanished_log_is_removed(self):
        LogCursor.objects.create(file_name="gone.example.com.access.log", offset=99)

        prune()

        self.assertEqual(LogCursor.objects.count(), 0)

    def test_a_cursor_for_a_present_log_survives(self):
        (self.directory / "here.example.com.access.log").write_text("")
        LogCursor.objects.create(file_name="here.example.com.access.log", offset=10)

        prune()

        self.assertEqual(LogCursor.objects.count(), 1)
