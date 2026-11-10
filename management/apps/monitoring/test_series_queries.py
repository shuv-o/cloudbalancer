"""
The dashboard's PromQL only names labels the exporter actually writes.

Every chart on the overview was blank in production while Nginx was serving
traffic, Prometheus was scraping it, and Django was getting 200 OK from every
query. The queries filtered on code="total"; nginx-module-vts writes one row
per response class and no aggregate row, so each selector matched nothing.

Nothing reported that. An empty instant vector stays empty through clamp_min()
and through division, so the result is not a zero that a chart would draw -- it
is no series at all, which renders identically to a gateway nobody has used.
That is the failure these tests exist to catch: a label value that silently
selects nothing.
"""
import re
from unittest import mock

from django.test import SimpleTestCase

from apps.monitoring import selectors

# What the exporter writes for `code`, taken from a live scrape of
# /status/format/prometheus. There is no "total", and never was.
VTS_CODE_VALUES = ("1xx", "2xx", "3xx", "4xx", "5xx")

# Matchers of the form code="x" or code=~"x", capturing the operator and value.
CODE_MATCHER = re.compile(r'code=(~?)"([^"]*)"')


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
                    if operator == "~":
                        pattern = re.compile(f"^(?:{value})$")
                        matched = [
                            code for code in VTS_CODE_VALUES if pattern.match(code)
                        ]
                        self.assertTrue(
                            matched,
                            f'code=~"{value}" matches none of {VTS_CODE_VALUES}, '
                            "so this chart will be blank whatever the traffic.",
                        )
                    else:
                        self.assertIn(
                            value,
                            VTS_CODE_VALUES,
                            f'code="{value}" is not a value the exporter writes.',
                        )

    def test_total_is_never_used_as_a_code(self):
        """The specific mistake, named, so a reintroduction reads clearly."""
        for query in collected_queries():
            self.assertNotIn(
                'code="total"',
                query,
                "nginx-module-vts emits no aggregate code row; sum the classes.",
            )


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
