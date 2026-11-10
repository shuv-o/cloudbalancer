"""
What the health probe actually asks the instance.

Every backend here is a reverse proxy that picks a site from the Host header.
Addressed by IP, such a proxy matches no route and answers 404 -- so a probe
without a Host header reports a healthy service as down, forever, and the panel
shows "0 of 1 up" beside a backend that is serving production traffic. That is
what these tests are about: the probe has to ask the question the operator
meant, not a question the backend cannot answer.
"""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.backends.health import matches as status_matches, parse_expected
from apps.backends.models import Backend
from apps.backends.services import (
    backend_create,
    health_check_backend,
    instance_add,
)


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class RecordingClient:
    """Stands in for httpx.Client and remembers how it was built and called."""

    calls = []

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        RecordingClient.calls.append({
            "url": url,
            "headers": headers,
            "verify": self.init_kwargs.get("verify"),
            "timeout": self.init_kwargs.get("timeout"),
        })
        return FakeResponse(200)


class ProbeRequestTests(TestCase):
    def setUp(self):
        RecordingClient.calls = []

    def _probe(self, **backend_kwargs):
        backend = backend_create(name="edge", **backend_kwargs)
        instance_add(backend=backend, address="10.0.0.1", port=80)
        with patch("apps.backends.services.httpx.Client", RecordingClient):
            health_check_backend(backend=backend)
        return RecordingClient.calls[-1]

    def test_host_header_is_sent_when_configured(self):
        call = self._probe(health_check_host="sms.example.com")
        self.assertEqual(call["headers"], {"Host": "sms.example.com"})

    def test_no_host_header_when_not_configured(self):
        call = self._probe()
        self.assertIsNone(call["headers"])

    def test_scheme_follows_the_backend(self):
        """An https backend probed over plaintext fails forever against a healthy host."""
        backend = backend_create(name="edge", scheme=Backend.Scheme.HTTPS)
        instance_add(backend=backend, address="10.0.0.1", port=443)

        with patch("apps.backends.services.httpx.Client", RecordingClient):
            health_check_backend(backend=backend)

        self.assertTrue(RecordingClient.calls[-1]["url"].startswith("https://"))

    def test_http_backend_still_probes_over_http(self):
        call = self._probe()
        self.assertTrue(call["url"].startswith("http://"))

    def test_url_carries_address_port_and_path(self):
        call = self._probe(health_check_path="/up")
        self.assertEqual(call["url"], "http://10.0.0.1:80/up")

    def test_verification_follows_tls_verify(self):
        """The probe dials an IP, so the certificate name cannot match by design."""
        backend = backend_create(name="edge", scheme=Backend.Scheme.HTTPS, tls_verify=False)
        instance_add(backend=backend, address="10.0.0.1", port=443)

        with patch("apps.backends.services.httpx.Client", RecordingClient):
            health_check_backend(backend=backend)

        self.assertFalse(RecordingClient.calls[-1]["verify"])


class HealthCheckHostValidationTests(TestCase):
    def test_a_bad_hostname_is_rejected(self):
        with self.assertRaises(ValidationError):
            backend_create(name="edge", health_check_host="not a hostname")

    def test_empty_is_allowed(self):
        backend = backend_create(name="edge", health_check_host="")
        self.assertEqual(backend.health_check_host, "")

    def test_a_real_hostname_is_kept(self):
        backend = backend_create(name="edge", health_check_host="sms.bdren.net.bd")
        self.assertEqual(backend.health_check_host, "sms.bdren.net.bd")


class ExpectedStatusTests(TestCase):
    """What the operator can declare healthy."""

    def test_the_default_accepts_2xx_and_3xx(self):
        self.assertTrue(status_matches("200-399", 200))
        self.assertTrue(status_matches("200-399", 302))
        self.assertFalse(status_matches("200-399", 404))
        self.assertFalse(status_matches("200-399", 500))

    def test_a_single_code_can_be_added(self):
        """A proxy, or an app with no /health route, answers 404 while working."""
        self.assertTrue(status_matches("200-399,404", 404))
        self.assertTrue(status_matches("200-399,404", 200))
        self.assertFalse(status_matches("200-399,404", 500))

    def test_only_one_code(self):
        self.assertTrue(status_matches("204", 204))
        self.assertFalse(status_matches("204", 200))

    def test_whitespace_is_forgiven(self):
        self.assertTrue(status_matches(" 200 - 299 , 404 ", 404))

    def test_a_backwards_range_is_rejected_with_the_fix_in_the_message(self):
        with self.assertRaises(ValidationError) as caught:
            parse_expected("399-200")
        self.assertIn("200-399", str(caught.exception))

    def test_nonsense_is_rejected(self):
        for spec in ["abc", "2oo", "200-", "-200", "200-abc"]:
            with self.subTest(spec=spec), self.assertRaises(ValidationError):
                parse_expected(spec)

    def test_a_code_outside_the_http_range_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse_expected("99")
        with self.assertRaises(ValidationError):
            parse_expected("600")

    def test_empty_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse_expected("   ")

    def test_an_unparseable_spec_falls_back_rather_than_condemning_everything(self):
        """Matching must not fail closed on something the operator cannot see."""
        self.assertTrue(status_matches("nonsense", 200))
        self.assertFalse(status_matches("nonsense", 500))

    def test_the_model_rejects_a_bad_spec(self):
        with self.assertRaises(ValidationError):
            backend_create(name="edge", health_check_expect="abc")

    def test_the_model_ignores_the_spec_for_a_tcp_check(self):
        """There is no status to compare, so the field is not in the way."""
        backend = backend_create(
            name="edge", health_check_method="tcp", health_check_expect="whatever",
        )
        self.assertEqual(backend.health_check_method, "tcp")


class TcpProbeTests(TestCase):
    """The check for a backend that has no health endpoint, or no HTTP at all."""

    def setUp(self):
        RecordingClient.calls = []
        self.backend = backend_create(name="edge", health_check_method="tcp")
        instance_add(backend=self.backend, address="10.0.0.1", port=5432)

    def test_a_connection_that_opens_is_healthy(self):
        with patch("apps.backends.services.socket.create_connection") as connect:
            results = health_check_backend(backend=self.backend)

        connect.assert_called_once()
        self.assertTrue(results[0].is_healthy)
        self.assertIsNone(results[0].status_code)

    def test_a_refused_connection_is_not(self):
        with patch(
            "apps.backends.services.socket.create_connection",
            side_effect=ConnectionRefusedError("refused"),
        ):
            results = health_check_backend(backend=self.backend)

        self.assertFalse(results[0].is_healthy)
        self.assertIn("refused", results[0].error)

    def test_no_http_request_is_made(self):
        with patch("apps.backends.services.socket.create_connection"), \
             patch("apps.backends.services.httpx.Client", RecordingClient):
            health_check_backend(backend=self.backend)

        self.assertEqual(RecordingClient.calls, [])


class ConfiguredStatusProbeTests(TestCase):
    """The configured codes have to reach the probe, not just the parser."""

    class Responds(RecordingClient):
        status = 404

        def get(self, url, headers=None):
            super().get(url, headers=headers)
            return FakeResponse(self.status)

    def setUp(self):
        RecordingClient.calls = []

    def _probe_with(self, expect):
        backend = backend_create(name="edge", health_check_expect=expect)
        instance_add(backend=backend, address="10.0.0.1", port=80)
        with patch("apps.backends.services.httpx.Client", self.Responds):
            return health_check_backend(backend=backend)[0]

    def test_a_404_is_unhealthy_by_default(self):
        self.assertFalse(self._probe_with("200-399").is_healthy)

    def test_a_404_is_healthy_when_the_operator_says_so(self):
        result = self._probe_with("200-399,404")
        self.assertTrue(result.is_healthy)
        self.assertEqual(result.status_code, 404)
