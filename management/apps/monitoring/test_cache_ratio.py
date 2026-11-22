"""
The hit ratio: one definition, and misses the backend refused left out of it.

A Laravel app behind the gateway showed 19%. Every page it serves carries a
session cookie and Cache-Control: no-cache, private, so Nginx -- correctly --
never stores one; the traffic module still reports each as a MISS, and those
misses buried the hits on the assets the cache was serving perfectly well.
"""
from unittest import mock

from django.test import SimpleTestCase, TestCase

from apps.monitoring import selectors
from apps.monitoring.selectors import cache_effectiveness, refused_misses
from apps.monitoring.test_series_queries import collected_queries


def vts(*, cache: dict, servers: dict | None = None, refused: dict | None = None) -> dict:
    """A traffic-status document with just the parts these selectors read."""
    return {
        "cacheZones": {"gateway_cache": {"responses": cache}},
        "serverZones": {
            name: {"requestCounter": sum(responses.values()), "responses": responses}
            for name, responses in (servers or {}).items()
        },
        "filterZones": {
            f"cache_refused::{host}": {"refused": {"requestCounter": n}}
            for host, n in (refused or {}).items()
        },
    }


class EffectivenessTests(SimpleTestCase):
    def test_refused_misses_leave_the_denominator(self):
        """The case that prompted this: 19 hits, 81 misses, 70 of them refused."""
        result = cache_effectiveness({"hit": 19, "miss": 81}, refused=70)
        self.assertEqual(result["cacheable_requests"], 30)
        self.assertAlmostEqual(result["hit_ratio"], 63.33)
        self.assertEqual(result["refused_by_backend"], 70)

    def test_without_refusals_nothing_changes(self):
        self.assertEqual(cache_effectiveness({"hit": 19, "miss": 81}, 0)["hit_ratio"], 19.0)

    def test_stale_and_revalidated_count_as_served(self):
        result = cache_effectiveness(
            {"hit": 5, "stale": 2, "revalidated": 3, "miss": 10}, 0
        )
        self.assertEqual(result["served_from_cache"], 10)
        self.assertEqual(result["hit_ratio"], 50.0)

    def test_bypassed_requests_are_never_in_it(self):
        self.assertEqual(
            cache_effectiveness({"hit": 1, "miss": 1, "bypass": 98}, 0)["hit_ratio"], 50.0
        )

    def test_refusals_cannot_exceed_misses(self):
        """Counters out of step must give a conservative ratio, never over 100%."""
        result = cache_effectiveness({"hit": 10, "miss": 5}, refused=50)
        self.assertEqual(result["refused_by_backend"], 5)
        self.assertEqual(result["hit_ratio"], 100.0)

    def test_everything_refused_is_no_ratio_rather_than_zero(self):
        """Nothing cacheable means there is no ratio to report, not a 0%."""
        self.assertIsNone(cache_effectiveness({"miss": 40}, refused=40)["hit_ratio"])


class RefusedCounterTests(SimpleTestCase):
    raw = vts(cache={}, refused={"a.example.com": 7, "b.example.com": 3})

    def test_all_domains(self):
        self.assertEqual(refused_misses(self.raw), 10)

    def test_one_domain(self):
        self.assertEqual(refused_misses(self.raw, "a.example.com"), 7)

    def test_a_domain_with_none(self):
        self.assertEqual(refused_misses(self.raw, "c.example.com"), 0)

    def test_other_filter_groups_are_ignored(self):
        raw = {"filterZones": {"host::a.example.com": {"a.example.com": {"requestCounter": 99}}}}
        self.assertEqual(refused_misses(raw), 0)

    def test_a_gateway_with_no_filters(self):
        self.assertEqual(refused_misses({}), 0)


class LiveFigureTests(TestCase):
    """The figures the dashboard shows, from one traffic-status document."""

    def setUp(self):
        self.raw = vts(
            cache={"hit": 19, "miss": 81, "bypass": 40},
            servers={
                "research.example.com": {"hit": 19, "miss": 81, "bypass": 40},
                "*": {"hit": 19, "miss": 81},
            },
            refused={"research.example.com": 70},
        )
        patcher = mock.patch.object(selectors, "vts_raw", return_value=self.raw)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_gateway_figure(self):
        stats = selectors.cache_stats()
        self.assertAlmostEqual(stats["hit_ratio"], 63.33)
        self.assertEqual(stats["refused_by_backend"], 70)
        self.assertEqual(stats["bypassed"], 40)

    def test_a_domain_agrees_with_the_gateway_about_the_same_traffic(self):
        """They used to differ: the domain row counted bypassed requests."""
        domain = next(
            d for d in selectors.traffic_stats()["domains"]
            if d["domain"] == "research.example.com"
        )
        self.assertEqual(domain["cache_hit_ratio"], selectors.cache_stats()["hit_ratio"])

    def test_unreachable_has_the_same_shape(self):
        with mock.patch.object(selectors, "vts_raw", return_value=None):
            self.assertEqual(selectors.cache_stats()["refused_by_backend"], 0)


class SeriesTests(SimpleTestCase):
    def _cache_query(self, **kwargs) -> str:
        return next(q for q in collected_queries(**kwargs) if "status=~\"hit|stale" in q)

    def test_the_chart_subtracts_refused_misses(self):
        query = self._cache_query()
        self.assertIn('nginx_vts_filter_requests_total{filter=~"cache_refused::.+"', query)

    def test_an_absent_refusal_series_does_not_blank_the_chart(self):
        self.assertIn("or vector(0)", self._cache_query())


class DomainSeriesTests(TestCase):
    def test_one_domain_subtracts_only_its_own(self):
        from apps.domains.services import domain_create

        domain_create(name="research.example.com")
        query = next(
            q for q in collected_queries(host="research.example.com")
            if "status=~\"hit|stale" in q
        )
        self.assertIn('filter="cache_refused::research.example.com"', query)
