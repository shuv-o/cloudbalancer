"""
The dashboard's PromQL only names metrics and labels the exporter writes.

Every chart on the overview was blank in production while Nginx was serving
traffic, Prometheus was scraping it, and Django was getting 200 OK from every
query. Two separate mistakes, the same shape:

  * the request queries filtered on code="total", and nginx-module-vts writes
    one row per response class with no aggregate row;
  * the cache query named nginx_vts_cache_total, which the exporter has never
    emitted under any version -- the real names are cache_requests_total, per
    zone, and server_cache_total, per vhost.

Nothing reported either. An empty instant vector stays empty through
clamp_min() and through division, so the result is not a zero that a chart
would draw -- it is no series at all, which renders identically to a gateway
nobody has used. A broken selector and an idle gateway look the same from the
browser, which is why both survived a deploy.

The vocabulary below is transcribed from the exporter's own format strings in
src/ngx_http_vhost_traffic_status_display_prometheus.h at v0.2.7, the version
gateway/nginx/Dockerfile pins. Pinning the version there and the names here is
what makes an upgrade that renames a metric fail loudly -- which is exactly
how the v0.2.4 to v0.2.7 bump was checked: the newer version adds histogram
buckets and a per-status-code counter and renames nothing the charts use.
"""
import re
from unittest import mock

from django.test import SimpleTestCase, TestCase

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.monitoring import selectors
from apps.routing.services import rule_create

# Every metric nginx-module-vts v0.2.7 emits.
VTS_METRICS = frozenset({
    "nginx_vts_info",
    "nginx_vts_start_time_seconds",
    "nginx_vts_main_connections",
    "nginx_vts_main_shm_usage_bytes",
    "nginx_vts_server_bytes_total",
    "nginx_vts_server_requests_total",
    "nginx_vts_server_request_seconds_total",
    "nginx_vts_server_request_seconds",
    "nginx_vts_server_cache_total",
    "nginx_vts_server_request_duration_seconds_bucket",
    "nginx_vts_server_request_duration_seconds_count",
    "nginx_vts_server_request_duration_seconds_sum",
    "nginx_vts_status_code_requests_total",
    "nginx_vts_filter_bytes_total",
    "nginx_vts_filter_requests_total",
    "nginx_vts_filter_request_seconds_total",
    "nginx_vts_filter_request_seconds",
    "nginx_vts_filter_cache_total",
    "nginx_vts_filter_request_duration_seconds_bucket",
    "nginx_vts_filter_request_duration_seconds_count",
    "nginx_vts_filter_request_duration_seconds_sum",
    "nginx_vts_upstream_bytes_total",
    "nginx_vts_upstream_requests_total",
    "nginx_vts_upstream_request_seconds_total",
    "nginx_vts_upstream_request_seconds",
    "nginx_vts_upstream_response_seconds_total",
    "nginx_vts_upstream_response_seconds",
    "nginx_vts_cache_bytes_total",
    "nginx_vts_cache_requests_total",
    "nginx_vts_cache_usage_bytes",
})

# What the exporter writes for `code`. There is no "total", and never was.
VTS_CODE_VALUES = ("1xx", "2xx", "3xx", "4xx", "5xx")

# What it writes for `status` on the two cache counters.
VTS_CACHE_STATUSES = (
    "miss", "bypass", "expired", "stale",
    "updating", "revalidated", "hit", "scarce",
)

METRIC_NAME = re.compile(r"\bnginx_vts_[a-z_]+")

# Matchers of the form code="x" or code=~"x", capturing the operator and value.
CODE_MATCHER = re.compile(r'code=(~?)"([^"]*)"')
STATUS_MATCHER = re.compile(r'status=(~?)"([^"]*)"')


def _selects_something(operator: str, value: str, vocabulary) -> list[str]:
    """Which of the exporter's values this matcher would actually select."""
    if operator == "~":
        pattern = re.compile(f"^(?:{value})$")
        return [known for known in vocabulary if pattern.match(known)]
    return [value] if value in vocabulary else []


def collected_queries(**kwargs) -> list[str]:
    """Every query traffic_series() would run, without running any of them."""
    seen: list[str] = []

    def record(query, **_kwargs):
        seen.append(query)
        return []

    with mock.patch.object(selectors, "_prometheus_range", side_effect=record):
        selectors.traffic_series(**kwargs)

    return seen


class CodeLabelTests(SimpleTestCase):
    def test_every_query_is_collected(self):
        """A guard on the guard: these tests are worthless against no queries."""
        queries = collected_queries()
        self.assertGreaterEqual(len(queries), 5)
        self.assertTrue(all("nginx_vts_" in query for query in queries))

    def test_no_query_matches_a_code_the_exporter_never_writes(self):
        for query in collected_queries():
            for operator, value in CODE_MATCHER.findall(query):
                with self.subTest(query=query, value=value):
                    self.assertTrue(
                        _selects_something(operator, value, VTS_CODE_VALUES),
                        f'code={operator}"{value}" matches none of '
                        f"{VTS_CODE_VALUES}, so this chart stays blank "
                        "whatever the traffic.",
                    )

    def test_total_is_never_used_as_a_code(self):
        """The specific mistake, named, so a reintroduction reads clearly."""
        for query in collected_queries():
            self.assertNotIn(
                'code="total"',
                query,
                "nginx-module-vts emits no aggregate code row; sum the classes.",
            )


class MetricNameTests(SimpleTestCase):
    def test_every_metric_named_is_one_the_exporter_emits(self):
        """
        The mistake that blanked the cache charts.

        A misspelt label at least selects a real metric. A metric name that was
        never written selects nothing at all, and Prometheus answers 200 OK
        with an empty result either way.
        """
        for query in collected_queries():
            for name in METRIC_NAME.findall(query):
                with self.subTest(query=query, metric=name):
                    self.assertIn(
                        name,
                        VTS_METRICS,
                        f"{name} is not emitted by nginx-module-vts v0.2.4.",
                    )

    def test_cache_queries_match_statuses_the_exporter_writes(self):
        for query in collected_queries():
            if "cache" not in query:
                continue
            matchers = STATUS_MATCHER.findall(query)
            with self.subTest(query=query):
                self.assertTrue(matchers, "a cache query with no status filter")
            for operator, value in matchers:
                with self.subTest(query=query, value=value):
                    self.assertTrue(
                        _selects_something(operator, value, VTS_CACHE_STATUSES),
                        f'status={operator}"{value}" matches none of '
                        f"{VTS_CACHE_STATUSES}.",
                    )

    def test_the_hit_ratio_agrees_with_the_live_figure(self):
        """
        One definition of 'served from cache', not two.

        cache_stats() counts hit, stale and revalidated: all three were
        answered without troubling a backend. A chart that counted only hits
        would sit below the number printed beside it, and the gap would look
        like a bug in the cache rather than in the query.
        """
        query = next(
            q for q in collected_queries()
            if "nginx_vts_cache_requests_total" in q
        )
        numerator = query.split("/ clamp_min(")[0]
        for status in ("hit", "stale", "revalidated"):
            with self.subTest(status=status):
                self.assertIn(status, numerator)


class HostLabelTests(SimpleTestCase):
    def test_per_domain_queries_drop_the_aggregate_and_the_catch_all(self):
        """
        Both exclusions matter, for different reasons.

        host="*" is the module's own total across every vhost: counting it
        beside the real ones doubles every figure. host="_" is the catch-all
        server, which is not a configured domain and answers without an
        upstream -- so its whole response time would be charged to the
        gateway's own overhead.
        """
        for query in collected_queries():
            if "nginx_vts_server_" not in query:
                continue
            with self.subTest(query=query):
                self.assertIn('host!="*"', query)
                self.assertIn('host!="_"', query)


class ResolutionTests(SimpleTestCase):
    """
    Every offered range has to be drawable.

    Prometheus rejects a range query asking for more than 11,000 points. A
    range the panel offers and Prometheus refuses is worse than one it does
    not offer: the chart fails only for the person who picks it.
    """

    UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

    def seconds(self, duration: str) -> int:
        return int(duration[:-1]) * self.UNITS[duration[-1]]

    def test_every_offered_range_stays_inside_the_point_budget(self):
        for offered in selectors.SERIES_RANGES:
            minutes = offered["minutes"]
            step, _window = selectors.resolution_for(minutes)
            points = minutes * 60 / self.seconds(step)
            with self.subTest(range=offered["label"], step=step):
                self.assertLess(
                    points,
                    11_000,
                    f"{offered['label']} at {step} is {points:.0f} points; "
                    "Prometheus refuses more than 11,000.",
                )
                self.assertGreater(
                    points, 50, f"{offered['label']} at {step} is too coarse to read."
                )

    def test_the_rate_window_is_never_narrower_than_the_step(self):
        """
        Otherwise the chart samples a slice and reports it as the whole.

        rate(...[2m]) read once a day describes two minutes out of every
        1,440 and draws it as the day. The window has to span the gap between
        points, not sit inside it.
        """
        for limit, step, window in selectors.SERIES_RESOLUTIONS:
            with self.subTest(limit=limit, step=step, window=window):
                self.assertGreaterEqual(self.seconds(window), self.seconds(step))

    def test_a_year_is_offered_and_capped(self):
        longest = max(r["minutes"] for r in selectors.SERIES_RANGES)
        self.assertGreaterEqual(longest, 365 * 24 * 60)
        self.assertLessEqual(longest, selectors.MAX_SERIES_MINUTES)


class DomainFilterTests(TestCase):
    """Filtering to one domain, which three of the five queries cannot do alone."""

    def setUp(self):
        self.domain = domain_create(name="shop.example.com")
        self.backend = backend_create(name="shop-api")
        instance_add(backend=self.backend, address="10.0.0.9", port=8080)
        rule_create(domain_id=self.domain.pk, backend_id=self.backend.pk)

    def test_server_queries_name_the_domain_instead_of_excluding_two(self):
        for query in collected_queries(host="shop.example.com"):
            if "nginx_vts_server_requests_total" not in query:
                continue
            with self.subTest(query=query):
                self.assertIn('host="shop.example.com"', query)
                self.assertNotIn('host!="*"', query)

    def test_cache_switches_to_the_metric_that_has_a_host_label(self):
        """
        The zone counter is shared between domains, so it cannot answer this.

        nginx_vts_cache_requests_total is labelled by cache_zone only. Filtering
        it by host is not possible; charting it unfiltered beside a domain name
        would be worse, because it would look filtered.
        """
        cache_queries = [
            q for q in collected_queries(host="shop.example.com") if "cache" in q
        ]
        self.assertTrue(cache_queries)
        for query in cache_queries:
            with self.subTest(query=query):
                self.assertIn("nginx_vts_server_cache_total", query)
                self.assertIn('host="shop.example.com"', query)
                self.assertNotIn("nginx_vts_cache_requests_total", query)

    def test_upstream_queries_are_narrowed_to_the_domain_s_own_upstreams(self):
        queries = collected_queries(host="shop.example.com")
        upstream_queries = [q for q in queries if "nginx_vts_upstream_" in q]
        self.assertTrue(upstream_queries)
        for query in upstream_queries:
            with self.subTest(query=query):
                self.assertIn('upstream=~"shop_api"', query)

    def test_a_domain_with_no_route_matches_no_upstream_rather_than_all(self):
        """
        An empty selector is not a narrower filter, it is no filter.

        upstream=~"" matches only the empty string, so a domain with nothing
        routed charts nothing. Omitting the selector entirely would chart every
        upstream on the gateway under that domain's name.
        """
        quiet = domain_create(name="parked.example.com")
        self.assertEqual(selectors.upstreams_serving(quiet.name), [])

        for query in collected_queries(host="parked.example.com"):
            if "nginx_vts_upstream_" not in query:
                continue
            with self.subTest(query=query):
                self.assertIn('upstream=~""', query)

    def test_a_fully_cached_domain_still_charts_its_overhead(self):
        """
        No upstream counter exists when nothing reaches a backend.

        Without `or vector(0)` the subtraction would yield an empty vector, and
        the gateway-overhead chart would go blank at exactly the moment the
        cache started working.
        """
        overhead = next(
            q for q in collected_queries(host="shop.example.com")
            if "nginx_vts_server_request_seconds_total" in q
        )
        self.assertIn("or vector(0)", overhead)

    def test_an_unconfigured_domain_is_refused_not_silently_empty(self):
        with mock.patch.object(selectors, "_prometheus_range", return_value=[]):
            with self.assertRaises(selectors.SeriesFilterError):
                selectors.traffic_series(host="not-configured.example.com")

    def test_a_label_value_cannot_close_the_selector(self):
        """
        The host is interpolated into PromQL, so it is validated, not escaped.

        Anything that is not a hostname is refused outright, which leaves no
        quote or brace to break out of the selector with.
        """
        for attempt in (
            'x","evil"="',
            "x}[1h]) or up{",
            'x\\"',
            "x y",
            "",
        ):
            with self.subTest(attempt=attempt):
                with mock.patch.object(
                    selectors, "_prometheus_range", return_value=[]
                ):
                    with self.assertRaises(selectors.SeriesFilterError):
                        selectors.traffic_series(host=attempt or "..")


class RangeClampTests(TestCase):
    def series(self, **kwargs) -> dict:
        with mock.patch.object(selectors, "_prometheus_range", return_value=[]):
            return selectors.traffic_series(**kwargs)

    def test_a_range_longer_than_retention_is_clamped_not_refused(self):
        """
        Asking for ten years is not an error, it is a request for everything.

        Refusing it would mean a caller had to know the retention window to
        ask a reasonable question.
        """
        series = self.series(minutes=10 * 365 * 24 * 60)
        self.assertEqual(series["range_minutes"], selectors.MAX_SERIES_MINUTES)

    def test_a_nonsensical_range_becomes_the_shortest_real_one(self):
        self.assertEqual(self.series(minutes=0)["range_minutes"], 1)
        self.assertEqual(self.series(minutes=-5)["range_minutes"], 1)


class WildcardDomainTests(TestCase):
    """
    A wildcard is a Domain like any other, and has to be chartable like one.

    Nginx matches *.example.com in server_name, and the traffic module labels
    the zone with the literal server_name -- so `*.example.com` is what
    Prometheus holds, and an exact match has to ask for it with the star
    intact.
    """

    def setUp(self):
        self.domain = domain_create(name="*.example.com")
        self.backend = backend_create(name="catch-all")
        instance_add(backend=self.backend, address="10.0.0.4", port=8080)
        rule_create(domain_id=self.domain.pk, backend_id=self.backend.pk)

    def test_a_wildcard_can_be_charted(self):
        for query in collected_queries(host="*.example.com"):
            if "nginx_vts_server_" not in query:
                continue
            with self.subTest(query=query):
                self.assertIn('host="*.example.com"', query)

    def test_the_star_does_not_reach_an_nginx_symbol(self):
        self.assertNotIn("*", self.domain.slug)

    def test_a_wildcard_slug_cannot_collide_with_a_real_domain(self):
        literal = domain_create(name="wildcard.example.com")
        self.assertNotEqual(self.domain.slug, literal.slug)

    def test_a_star_anywhere_but_the_front_is_still_refused(self):
        for attempt in ("*", "*.", "*x.example.com", "a.*.com"):
            with self.subTest(attempt=attempt):
                with mock.patch.object(
                    selectors, "_prometheus_range", return_value=[]
                ):
                    with self.assertRaises(selectors.SeriesFilterError):
                        selectors.traffic_series(host=attempt)
