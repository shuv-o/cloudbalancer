"""
What each role may read.

The split that matters: aggregates are viewer-readable, complete addresses
are not. Knowing which countries and paths a gateway serves is part of
running it and identifies nobody. A list of IPs is personal data kept for
thirty days to investigate abuse, which is an operator's job rather than
something visible to anyone who can open a dashboard.

That split is on the endpoint rather than on a field, so there is no
combination of parameters by which a viewer receives an address. These tests
say so.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.analytics.models import AgentDay, ClientDay, GeoDay, HourBucket, PathDay
from apps.security.roles import Role
from apps.security.testing import make_operator


def seed(days_ago: int = 1, domain: str = "example.com", ip: str = "203.0.113.7"):
    when = timezone.now().date() - timedelta(days=days_ago)
    ClientDay.objects.create(
        date=when, domain=domain, ip=ip,
        country="BD", country_name="Bangladesh", city="Dhaka",
        latitude=23.8, longitude=90.4, asn=63969, asn_name="BdREN",
        requests=120, bytes_sent=9000, errors=4, server_errors=1,
        total_ms=4800, last_seen=timezone.now(),
    )
    GeoDay.objects.create(
        date=when, domain=domain, country="BD", country_name="Bangladesh",
        asn=63969, asn_name="BdREN", clients=1,
        requests=120, bytes_sent=9000, errors=4,
    )
    PathDay.objects.create(
        date=when, domain=domain, path="/research/{n}",
        requests=120, bytes_sent=9000, errors=4, server_errors=1,
        cache_hits=60, total_ms=4800, max_ms=900,
    )
    AgentDay.objects.create(
        date=when, domain=domain, kind=AgentDay.Kind.BROWSER,
        example="Mozilla/5.0", requests=100, bytes_sent=8000,
    )
    HourBucket.objects.create(
        date=when, hour=10, domain=domain,
        requests=120, bytes_sent=9000, errors=4, total_ms=4800,
    )


class PermissionTests(TestCase):
    def setUp(self):
        seed()

    def test_a_viewer_reads_the_aggregates(self):
        self.client.force_login(make_operator(username="v", superuser=False, role=Role.VIEWER))

        for path in (
            "/api/v1/analytics/",
            "/api/v1/analytics/geography/",
            "/api/v1/analytics/paths/",
            "/api/v1/analytics/heatmap/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_a_viewer_cannot_read_client_addresses(self):
        """The one endpoint that returns personal data."""
        self.client.force_login(make_operator(username="v", superuser=False, role=Role.VIEWER))
        self.assertEqual(self.client.get("/api/v1/analytics/clients/").status_code, 403)

    def test_an_operator_can(self):
        self.client.force_login(make_operator(username="o", superuser=False, role=Role.OPERATOR))
        self.assertEqual(self.client.get("/api/v1/analytics/clients/").status_code, 200)

    def test_no_aggregate_endpoint_leaks_an_address(self):
        """
        The split is on the endpoint, so this should hold by construction.
        Asserted anyway, because by-construction is a claim until something
        checks it.
        """
        self.client.force_login(make_operator(username="v", superuser=False, role=Role.VIEWER))

        for path in (
            "/api/v1/analytics/",
            "/api/v1/analytics/geography/",
            "/api/v1/analytics/paths/",
            "/api/v1/analytics/heatmap/",
        ):
            with self.subTest(path=path):
                self.assertNotIn("203.0.113.7", self.client.get(path).content.decode())

    def test_fetching_geoip_databases_is_admin_only(self):
        self.client.force_login(make_operator(username="o", superuser=False, role=Role.OPERATOR))
        self.assertEqual(self.client.get("/api/v1/analytics/geoip/").status_code, 403)

        self.client.force_login(make_operator(username="boss", superuser=True))
        self.assertEqual(self.client.get("/api/v1/analytics/geoip/").status_code, 200)


class ContentTests(TestCase):
    def setUp(self):
        self.client.force_login(make_operator(username="o", superuser=False, role=Role.OPERATOR))

    def test_the_summary_totals_the_window(self):
        seed(days_ago=1)
        seed(days_ago=2)

        body = self.client.get("/api/v1/analytics/?days=7").json()["summary"]

        self.assertEqual(body["requests"], 240)
        self.assertEqual(body["errors"], 8)
        self.assertEqual(body["countries"], 1)
        self.assertEqual(body["networks"], 1)

    def test_a_window_excludes_what_is_outside_it(self):
        seed(days_ago=1)
        seed(days_ago=20)

        body = self.client.get("/api/v1/analytics/?days=7").json()["summary"]
        self.assertEqual(body["requests"], 120)

    def test_a_domain_filter_narrows_everything(self):
        seed(domain="a.example.com")
        seed(domain="b.example.com", ip="203.0.113.8")

        body = self.client.get("/api/v1/analytics/?domain=a.example.com").json()
        self.assertEqual(body["summary"]["requests"], 120)

        clients = self.client.get(
            "/api/v1/analytics/clients/?domain=a.example.com"
        ).json()["clients"]
        self.assertEqual([c["ip"] for c in clients], ["203.0.113.7"])

    def test_clients_carry_their_location(self):
        seed()

        client = self.client.get("/api/v1/analytics/clients/").json()["clients"][0]

        self.assertEqual(client["country"], "BD")
        self.assertEqual(client["asn"], 63969)
        self.assertEqual(client["asn_name"], "BdREN")
        self.assertEqual(client["requests"], 120)

    def test_a_year_of_clients_is_capped_at_the_retention_and_says_so(self):
        """
        "No clients in the last year" and "we only keep thirty days of
        addresses" are different answers, and only one of them is true.
        """
        seed()

        body = self.client.get("/api/v1/analytics/clients/?days=365").json()

        self.assertTrue(body["truncated_to_retention"])
        self.assertEqual(body["days"], body["retention_days"])

    def test_geography_separates_countries_from_networks(self):
        seed()

        body = self.client.get("/api/v1/analytics/geography/").json()

        self.assertEqual(body["countries"][0]["country"], "BD")
        self.assertEqual(body["networks"][0]["asn"], 63969)

    def test_unlocated_traffic_is_reported_rather_than_dropped(self):
        """A map that silently omits a third of the traffic is worse than one that says so."""
        seed()
        GeoDay.objects.create(
            date=timezone.now().date(), domain="example.com", country="",
            requests=50, clients=1,
        )

        body = self.client.get("/api/v1/analytics/geography/").json()
        self.assertEqual(body["unlocated_requests"], 50)

    def test_paths_are_ranked_three_ways(self):
        seed()

        body = self.client.get("/api/v1/analytics/paths/").json()

        self.assertEqual(body["busiest"][0]["path"], "/research/{n}")
        self.assertAlmostEqual(body["busiest"][0]["average_ms"], 40.0)
        self.assertAlmostEqual(body["busiest"][0]["cache_hit_ratio"], 50.0)
        self.assertEqual(body["failing"][0]["errors"], 4)

    def test_the_slowest_list_ranks_by_time_per_request(self):
        """
        Not by total time. A path served a million times at 2ms would
        otherwise outrank the one taking nine seconds, which is the one
        somebody needs to see.
        """
        when = timezone.now().date()
        PathDay.objects.create(
            date=when, domain="example.com", path="/fast",
            requests=1_000_000, total_ms=2_000_000, max_ms=5,
        )
        PathDay.objects.create(
            date=when, domain="example.com", path="/slow",
            requests=10, total_ms=90_000, max_ms=12_000,
        )

        slowest = self.client.get("/api/v1/analytics/paths/").json()["slowest"]
        self.assertEqual(slowest[0]["path"], "/slow")

    def test_a_barely_used_path_does_not_top_the_slowest_list(self):
        """One request that took four seconds is not a performance problem."""
        when = timezone.now().date()
        PathDay.objects.create(
            date=when, domain="example.com", path="/once",
            requests=1, total_ms=4000, max_ms=4000,
        )
        PathDay.objects.create(
            date=when, domain="example.com", path="/often",
            requests=50, total_ms=50_000, max_ms=1200,
        )

        slowest = self.client.get("/api/v1/analytics/paths/").json()["slowest"]
        self.assertEqual([row["path"] for row in slowest], ["/often"])

    def test_the_heatmap_averages_rather_than_sums(self):
        """
        Otherwise a four-week window is just four times darker than a
        one-week window and says nothing new.
        """
        for week in range(4):
            HourBucket.objects.create(
                date=timezone.now().date() - timedelta(days=7 * week),
                hour=14, domain="example.com", requests=100,
            )

        body = self.client.get("/api/v1/analytics/heatmap/?days=28").json()
        cell = next(c for c in body["cells"] if c["hour"] == 14)

        self.assertEqual(cell["requests"], 400)
        self.assertEqual(cell["average_requests"], 100.0)

    def test_agent_shares_add_up(self):
        seed()
        AgentDay.objects.create(
            date=timezone.now().date(), domain="example.com",
            kind=AgentDay.Kind.BOT, requests=100,
        )

        kinds = self.client.get("/api/v1/analytics/").json()["agents"]["kinds"]
        self.assertAlmostEqual(sum(k["share"] for k in kinds), 100.0, places=1)

    def test_an_empty_installation_answers_rather_than_erroring(self):
        """Every one of these runs before a single log line has been read."""
        for path in (
            "/api/v1/analytics/",
            "/api/v1/analytics/clients/",
            "/api/v1/analytics/geography/",
            "/api/v1/analytics/paths/",
            "/api/v1/analytics/heatmap/",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_a_nonsense_range_does_not_error(self):
        for query in ("?days=abc", "?days=-5", "?days=99999", "?days="):
            with self.subTest(query=query):
                self.assertEqual(
                    self.client.get(f"/api/v1/analytics/{query}").status_code, 200
                )
