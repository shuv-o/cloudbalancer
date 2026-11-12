"""
The configuration preview, which answered 404 for the query string it was
written to handle.

The panel asked for /api/v1/gateway/config/?format=combined. `format` is
reserved: DRF's URL_FORMAT_OVERRIDE defaults to it, so the framework reads the
value as the name of a renderer, finds none called "combined", and raises
NotFound before the view runs. The endpoint existed, the branch existed, and
the button had never worked -- and a 404 reads as a missing route, which is the
one thing it was not.

Nothing caught it because nothing ever asked for the combined form. So these
ask for both forms, through the URL, as the panel does.
"""
from django.test import TestCase
from rest_framework.settings import api_settings

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.routing.services import rule_create
from apps.security.roles import Role
from apps.security.testing import make_operator

URL = "/api/v1/gateway/config/"


class ConfigPreviewTests(TestCase):
    def setUp(self):
        backend = backend_create(name="api")
        instance_add(backend=backend, address="10.0.0.1", port=8080)
        domain = domain_create(name="preview.example.com")
        rule_create(domain_id=domain.pk, backend_id=backend.pk)

        self.client.force_login(
            make_operator(username="op", superuser=False, role=Role.OPERATOR)
        )

    def test_the_default_form_lists_each_generated_file(self):
        response = self.client.get(URL)

        self.assertEqual(response.status_code, 200)
        files = response.json()["files"]
        self.assertIn("20-servers.conf", files)
        self.assertIn("preview.example.com", files["20-servers.conf"])

    def test_the_combined_form_returns_one_document(self):
        """The exact request the panel's preview button makes."""
        response = self.client.get(f"{URL}?combined=1")

        self.assertEqual(response.status_code, 200, response.content)
        config = response.json()["config"]
        self.assertIn("preview.example.com", config)
        self.assertIn("20-servers.conf", config)

    def test_the_combined_flag_accepts_what_a_caller_would_send(self):
        for value in ("1", "true", "yes", "TRUE"):
            with self.subTest(value=value):
                response = self.client.get(f"{URL}?combined={value}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("config", response.json())

    def test_anything_else_falls_back_to_the_file_listing(self):
        for value in ("0", "false", "", "maybe"):
            with self.subTest(value=value):
                response = self.client.get(f"{URL}?combined={value}")
                self.assertEqual(response.status_code, 200)
                self.assertIn("files", response.json())

    def test_the_parameter_is_not_one_drf_has_claimed(self):
        """
        The actual defect, named.

        DRF reads URL_FORMAT_OVERRIDE out of the query string and resolves it
        to a renderer. Any application parameter sharing that name turns an
        unrecognised value into a 404 from the framework, which no amount of
        reading the view will explain.
        """
        reserved = api_settings.URL_FORMAT_OVERRIDE
        self.assertNotEqual(
            reserved,
            "combined",
            "the preview parameter now collides with DRF's own",
        )

        # And the collision it used to have still behaves the way it did,
        # which is why the parameter had to move rather than be fixed.
        response = self.client.get(f"{URL}?{reserved}=combined")
        self.assertEqual(response.status_code, 404)

    def test_a_viewer_can_preview_but_not_deploy(self):
        """Reading the configuration is a read; writing it is not."""
        self.client.force_login(
            make_operator(username="v", superuser=False, role=Role.VIEWER)
        )

        self.assertEqual(self.client.get(f"{URL}?combined=1").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/gateway/deploy/").status_code, 403)
