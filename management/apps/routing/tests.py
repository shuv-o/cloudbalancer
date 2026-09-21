"""
API-level tests.

These go through the HTTP layer rather than calling services directly, because
the things most likely to break are the seams: permissions, the deploy trigger
that every write is supposed to fire, and whether an error reaches the operator
in a form they can act on.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.routing.models import RoutingRule
from apps.security.testing import make_operator


class AuthenticationTests(TestCase):
    def test_management_api_refuses_anonymous_callers(self):
        """The panel changes what the gateway does; it is not a public API."""
        for path in [
            "/api/v1/domains/",
            "/api/v1/backends/",
            "/api/v1/routing/rules/",
            "/api/v1/certificates/",
            "/api/v1/monitoring/overview/",
            "/api/v1/gateway/deploy/",
        ]:
            with self.subTest(path=path):
                self.assertIn(self.client.get(path).status_code, (401, 403))

    def test_signing_in_and_out(self):
        make_operator(
            username="operator",
            password="correct-horse",
            superuser=False,
            with_second_factor=False,
        )

        self.assertEqual(
            self.client.post(
                "/api/v1/auth/login/",
                {"username": "operator", "password": "wrong"},
                content_type="application/json",
            ).status_code,
            401,
        )

        response = self.client.post(
            "/api/v1/auth/login/",
            {"username": "operator", "password": "correct-horse"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "operator")

        self.assertTrue(self.client.get("/api/v1/auth/me/").json()["authenticated"])
        self.client.post("/api/v1/auth/logout/")
        self.assertFalse(self.client.get("/api/v1/auth/me/").json()["authenticated"])

    def test_a_wrong_password_does_not_say_which_part_was_wrong(self):
        make_operator(
            username="operator",
            password="correct-horse",
            superuser=False,
            with_second_factor=False,
        )
        response = self.client.post(
            "/api/v1/auth/login/",
            {"username": "operator", "password": "wrong"},
            content_type="application/json",
        )
        self.assertEqual(
            response.json()["error"],
            "That username and password do not match an account.",
        )


class DeployTriggerTests(TestCase):
    """
    Every write has to reach the gateway.

    A change saved in the database but never rendered is the worst kind of bug
    here: the panel says one thing and the gateway does another.
    """

    def setUp(self):
        self.user = make_operator(username="admin")
        self.client.force_login(self.user)
        self.backend = backend_create(name="api-service")
        instance_add(backend=self.backend, address="10.0.1.11", port=8080)
        self.domain = domain_create(name="api.example.com")

    @patch("apps.domains.apis.request_config_deploy")
    def test_creating_a_domain_deploys(self, deploy):
        self.client.post(
            "/api/v1/domains/",
            {"name": "new.example.com"},
            content_type="application/json",
        )
        deploy.assert_called_once()

    @patch("apps.routing.apis.request_config_deploy")
    def test_creating_a_rule_deploys(self, deploy):
        response = self.client.post(
            "/api/v1/routing/rules/",
            {
                "domain_id": self.domain.id,
                "backend_id": self.backend.id,
                "match_value": "/v1/",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)
        deploy.assert_called_once()

    @patch("apps.routing.apis.request_config_deploy")
    def test_deleting_a_rule_deploys(self, deploy):
        rule = RoutingRule.objects.create(
            domain=self.domain, backend=self.backend, match_value="/v1/"
        )
        self.client.delete(f"/api/v1/routing/rules/{rule.id}/")
        deploy.assert_called_once()

    @patch("apps.backends.apis.request_config_deploy")
    def test_adding_an_instance_deploys(self, deploy):
        self.client.post(
            f"/api/v1/backends/{self.backend.id}/instances/",
            {"address": "10.0.1.12", "port": 8080},
            content_type="application/json",
        )
        deploy.assert_called_once()


class RuleApiTests(TestCase):
    def setUp(self):
        self.user = make_operator(username="admin")
        self.client.force_login(self.user)
        self.backend = backend_create(name="api-service")
        instance_add(backend=self.backend, address="10.0.1.11", port=8080)
        self.canary = backend_create(name="api-canary")
        instance_add(backend=self.canary, address="10.0.1.99", port=8080)
        self.domain = domain_create(name="api.example.com")

    @patch("apps.routing.apis.request_config_deploy")
    def test_unsafe_cache_configuration_is_rejected_with_a_usable_message(self, _deploy):
        response = self.client.post(
            "/api/v1/routing/rules/",
            {
                "domain_id": self.domain.id,
                "backend_id": self.backend.id,
                "match_value": "/v1/",
                "cache_enabled": True,
                "cache_bypass_auth": False,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("cache_bypass_auth", response.json())
        self.assertIn("one user's data to another", response.json()["cache_bypass_auth"][0])

    @patch("apps.routing.apis.request_config_deploy")
    def test_header_override_is_returned_with_its_rule(self, _deploy):
        rule = self.client.post(
            "/api/v1/routing/rules/",
            {
                "domain_id": self.domain.id,
                "backend_id": self.backend.id,
                "match_value": "/v1/",
            },
            content_type="application/json",
        ).json()

        self.client.post(
            f"/api/v1/routing/rules/{rule['id']}/header-routes/",
            {"backend_id": self.canary.id, "header_value": "canary"},
            content_type="application/json",
        )

        listed = self.client.get("/api/v1/routing/rules/").json()
        self.assertEqual(len(listed[0]["header_routes"]), 1)
        self.assertEqual(listed[0]["header_routes"][0]["backend_name"], "api-canary")

    @patch("apps.routing.apis.request_config_deploy")
    def test_rules_can_be_filtered_by_domain(self, _deploy):
        other = domain_create(name="other.example.com")
        for domain in (self.domain, other):
            RoutingRule.objects.create(
                domain=domain, backend=self.backend, match_value="/"
            )

        listed = self.client.get(f"/api/v1/routing/rules/?domain_id={self.domain.id}").json()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["domain_name"], "api.example.com")


class MonitoringApiTests(TestCase):
    def setUp(self):
        self.user = make_operator(username="admin")
        self.client.force_login(self.user)

    def test_responses_keep_their_shape_when_nginx_is_unreachable(self):
        """
        A payload that loses fields when a dependency is down forces every
        consumer to guard every field, and the first one to forget breaks.
        """
        response = self.client.get("/api/v1/monitoring/overview/")
        self.assertEqual(response.status_code, 200)

        payload = response.json()
        self.assertFalse(payload["traffic"]["available"])
        self.assertEqual(payload["traffic"]["domains"], [])
        self.assertEqual(payload["upstreams"]["upstreams"], [])
        self.assertIn("hit", payload["cache"]["breakdown"])
        self.assertIsNone(payload["cache"]["hit_ratio"])

    def test_summary_works_on_an_empty_install(self):
        payload = self.client.get("/api/v1/monitoring/summary/").json()
        self.assertEqual(payload["domains"]["total"], 0)
        self.assertEqual(payload["certificates"]["total"], 0)
