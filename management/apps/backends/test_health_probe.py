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
