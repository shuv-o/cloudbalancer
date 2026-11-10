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
src/ngx_http_vhost_traffic_status_display_prometheus.h at v0.2.4, the version
gateway/nginx/Dockerfile pins. Pinning the version there and the names here is
what makes an upgrade that renames a metric fail loudly.
"""
import re
from unittest import mock

from django.test import SimpleTestCase

from apps.monitoring import selectors

# Every metric nginx-module-vts v0.2.4 emits. Histogram buckets omitted: this
# stack does not configure them, so they never appear in a scrape.
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
    "nginx_vts_filter_bytes_total",
    "nginx_vts_filter_requests_total",
    "nginx_vts_filter_request_seconds_total",
    "nginx_vts_filter_request_seconds",
    "nginx_vts_filter_cache_total",
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


def collected_queries() -> list[str]:
    """Every query traffic_series() would run, without running any of them."""
    seen: list[str] = []

    def record(query, **_kwargs):
        seen.append(query)
        return []

    with mock.patch.object(selectors, "_prometheus_range", side_effect=record):
        selectors.traffic_series()

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
