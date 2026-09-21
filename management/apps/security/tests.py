"""
Tests for the controls protecting the control panel.

These matter more than most: every other test here guards against an outage,
and these guard against someone else running the gateway.
"""
import re
from datetime import timedelta
from unittest.mock import patch

import pyotp
from django.test import TestCase
from django.utils import timezone

from apps.certificates.services import certificate_generate_self_signed
from apps.domains.services import domain_create
from apps.gateway.services import config_render
from apps.security.models import (
    AccountLock,
    AuditEvent,
    LoginAttempt,
    PanelAccessPolicy,
    TotpDevice,
)
from apps.security.services import (
    SecurityError,
    ip_is_allowed,
    policy_update,
    totp_begin_enrollment,
    totp_confirm_enrollment,
    totp_verify,
)
from apps.security.testing import current_code, make_operator


class SecondFactorTests(TestCase):
    def setUp(self):
        self.user = make_operator(with_second_factor=False)

    def test_enrollment_does_not_take_effect_until_confirmed(self):
        """
        A half-finished setup must not change how signing in works, or closing
        the tab would lock the operator out of their own gateway.
        """
        totp_begin_enrollment(user=self.user)
        device = TotpDevice.objects.get(user=self.user)

        self.assertFalse(device.is_confirmed)
        self.assertFalse(totp_verify(user=self.user, code=pyotp.TOTP(device.secret).now()))

    def test_confirming_returns_recovery_codes_once(self):
        totp_begin_enrollment(user=self.user)
        device = TotpDevice.objects.get(user=self.user)

        codes = totp_confirm_enrollment(user=self.user, code=pyotp.TOTP(device.secret).now())
        device.refresh_from_db()

        self.assertEqual(len(codes), 10)
        self.assertTrue(device.is_confirmed)
        # Only hashes are kept, so a database dump is not a set of working codes.
        for code in codes:
            self.assertNotIn(code, device.recovery_codes)

    def test_a_wrong_code_does_not_confirm(self):
        totp_begin_enrollment(user=self.user)
        with self.assertRaises(SecurityError):
            totp_confirm_enrollment(user=self.user, code="000000")

    def test_a_code_cannot_be_used_twice(self):
        """
        A TOTP code stays valid for its whole thirty-second step, so without
        recording the step an observed code can be replayed inside that window.
        """
        user = make_operator(username="replay")
        code = current_code(user)

        self.assertTrue(totp_verify(user=user, code=code))
        self.assertFalse(totp_verify(user=user, code=code))

    def test_a_recovery_code_works_once(self):
        totp_begin_enrollment(user=self.user)
        device = TotpDevice.objects.get(user=self.user)
        codes = totp_confirm_enrollment(user=self.user, code=pyotp.TOTP(device.secret).now())

        self.assertTrue(totp_verify(user=self.user, code=codes[0]))
        self.assertFalse(totp_verify(user=self.user, code=codes[0]))

        device.refresh_from_db()
        self.assertEqual(device.recovery_codes_remaining, 9)

    def test_restarting_enrollment_issues_a_new_secret(self):
        """An abandoned attempt should leave nothing usable behind."""
        first = totp_begin_enrollment(user=self.user)["secret"]
        second = totp_begin_enrollment(user=self.user)["secret"]
        self.assertNotEqual(first, second)


class SignInTests(TestCase):
    """The sign-in endpoint, which is the surface an attacker actually meets."""

    def setUp(self):
        self.password = "correct-horse-battery"
        self.user = make_operator(username="operator", password=self.password)

    def _post(self, **kwargs):
        return self.client.post(
            "/api/v1/auth/login/",
            {"username": "operator", **kwargs},
            content_type="application/json",
        )

    def test_a_password_alone_is_not_enough(self):
        response = self._post(password=self.password)

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["totp_required"])
        # Not counted as a failed attempt: the password was right and the panel
        # is simply asking for the second factor.
        self.assertFalse(LoginAttempt.objects.exists())

    def test_password_and_code_together_sign_in(self):
        response = self._post(password=self.password, otp=current_code(self.user))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "operator")
        self.assertEqual(
            LoginAttempt.objects.get().outcome, LoginAttempt.Outcome.SUCCESS
        )

    def test_a_wrong_code_is_refused_and_recorded(self):
        response = self._post(password=self.password, otp="000000")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            LoginAttempt.objects.get().outcome, LoginAttempt.Outcome.BAD_TOTP
        )

    def test_a_wrong_password_reveals_nothing_about_the_account(self):
        real = self._post(password="wrong").json()["error"]
        unknown = self.client.post(
            "/api/v1/auth/login/",
            {"username": "nobody", "password": "wrong"},
            content_type="application/json",
        ).json()["error"]

        self.assertEqual(real, unknown)

    def test_the_session_key_changes_on_sign_in(self):
        """
        A session fixed before authentication must not still be valid after it.
        """
        self.client.get("/api/v1/auth/csrf/")
        before = self.client.session.session_key

        self._post(password=self.password, otp=current_code(self.user))

        self.assertNotEqual(before, self.client.session.session_key)


class LockoutTests(TestCase):
    def setUp(self):
        self.password = "correct-horse-battery"
        self.user = make_operator(username="operator", password=self.password)

    def _fail(self):
        return self.client.post(
            "/api/v1/auth/login/",
            {"username": "operator", "password": "wrong"},
            content_type="application/json",
        )

    def test_repeated_failures_lock_the_account(self):
        policy = PanelAccessPolicy.load()

        for _ in range(policy.lockout_threshold):
            self._fail()

        response = self._fail()
        self.assertEqual(response.status_code, 429)
        self.assertTrue(response.json()["locked"])

    def test_a_locked_account_refuses_even_the_right_password(self):
        """
        Checked before the password, so a lock cannot be used as an oracle for
        whether a guess was correct.
        """
        policy = PanelAccessPolicy.load()
        for _ in range(policy.lockout_threshold):
            self._fail()

        response = self.client.post(
            "/api/v1/auth/login/",
            {
                "username": "operator",
                "password": self.password,
                "otp": current_code(self.user),
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)

    def test_signing_in_clears_the_count(self):
        self._fail()
        self.client.post(
            "/api/v1/auth/login/",
            {
                "username": "operator",
                "password": self.password,
                "otp": current_code(self.user),
            },
            content_type="application/json",
        )
        self.assertFalse(AccountLock.objects.filter(username="operator").exists())

    def test_locking_can_be_turned_off(self):
        policy = PanelAccessPolicy.load()
        policy.lockout_threshold = 0
        policy.save()

        for _ in range(8):
            response = self._fail()

        self.assertEqual(response.status_code, 401)


class AllowlistTests(TestCase):
    def test_an_empty_list_allows_everything(self):
        policy = PanelAccessPolicy.load()
        self.assertTrue(ip_is_allowed(policy=policy, address="198.51.100.9"))

    def test_only_listed_ranges_are_allowed(self):
        policy = PanelAccessPolicy.load()
        policy.ip_allowlist = ["203.0.113.0/24", "198.51.100.17"]
        policy.save()

        self.assertTrue(ip_is_allowed(policy=policy, address="203.0.113.44"))
        self.assertTrue(ip_is_allowed(policy=policy, address="198.51.100.17"))
        self.assertFalse(ip_is_allowed(policy=policy, address="192.0.2.1"))
        self.assertFalse(ip_is_allowed(policy=policy, address=None))

    def test_a_malformed_entry_is_dropped_rather_than_rendered(self):
        """An unparseable CIDR in the config would fail nginx -t on every deploy."""
        policy = PanelAccessPolicy.load()
        policy.ip_allowlist = ["203.0.113.0/24", "not-an-address", ""]
        policy.save()

        self.assertEqual(policy.normalised_allowlist, ["203.0.113.0/24"])

    def test_requests_from_outside_the_list_are_refused(self):
        policy = PanelAccessPolicy.load()
        policy.ip_allowlist = ["203.0.113.0/24"]
        policy.save()

        blocked = self.client.get("/api/v1/auth/me/", REMOTE_ADDR="192.0.2.1")
        allowed = self.client.get("/api/v1/auth/me/", REMOTE_ADDR="203.0.113.5")

        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(allowed.status_code, 200)


class EnrollmentEnforcementTests(TestCase):
    def test_an_operator_without_a_second_factor_is_held_at_enrollment(self):
        """
        Enforced on every request, not only at sign-in — otherwise turning the
        requirement on would do nothing for accounts that already existed.
        """
        user = make_operator(with_second_factor=False)
        self.client.force_login(user)

        blocked = self.client.get("/api/v1/domains/")
        self.assertEqual(blocked.status_code, 403)
        self.assertTrue(blocked.json()["totp_setup_required"])

        # The enrollment endpoints themselves stay reachable.
        self.assertEqual(self.client.post("/api/v1/security/totp/setup/").status_code, 200)

    def test_the_requirement_can_be_turned_off(self):
        policy = PanelAccessPolicy.load()
        policy.require_totp = False
        policy.save()

        self.client.force_login(make_operator(with_second_factor=False))
        self.assertEqual(self.client.get("/api/v1/domains/").status_code, 200)


class SessionExpiryTests(TestCase):
    def setUp(self):
        self.user = make_operator()
        self.client.force_login(self.user)

    def test_an_idle_session_ends(self):
        policy = PanelAccessPolicy.load()

        session = self.client.session
        session["last_seen_at"] = (
            timezone.now() - timedelta(minutes=policy.session_idle_minutes + 5)
        ).timestamp()
        session["started_at"] = timezone.now().timestamp()
        session.save()

        response = self.client.get("/api/v1/domains/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["reason"], "idle")

    def test_a_session_ends_at_its_maximum_age_however_active(self):
        policy = PanelAccessPolicy.load()

        session = self.client.session
        session["started_at"] = (
            timezone.now() - timedelta(hours=policy.session_max_hours + 1)
        ).timestamp()
        session["last_seen_at"] = timezone.now().timestamp()
        session.save()

        response = self.client.get("/api/v1/domains/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["reason"], "age")


class AuditTests(TestCase):
    def setUp(self):
        self.user = make_operator(username="admin")
        self.client.force_login(self.user)

    @patch("apps.domains.apis.request_config_deploy")
    def test_changes_are_recorded_with_who_made_them(self, _deploy):
        self.client.post(
            "/api/v1/domains/",
            {"name": "recorded.example.com"},
            content_type="application/json",
        )

        event = AuditEvent.objects.filter(path="/api/v1/domains/").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.actor_name, "admin")
        self.assertEqual(event.summary, "Added a domain")

    def test_reads_are_not_recorded(self):
        """Recording every read would bury the changes in noise."""
        self.client.get("/api/v1/domains/")
        self.assertFalse(AuditEvent.objects.filter(action="GET").exists())


class PolicyTests(TestCase):
    def test_publishing_without_a_certificate_is_refused(self):
        """
        Over plain HTTP the panel's session cookie is visible to anyone on the
        path, and that cookie controls every domain this gateway serves.
        """
        domain_create(name="control.example.com")

        with self.assertRaises(SecurityError) as caught:
            policy_update(
                policy=PanelAccessPolicy.load(),
                data={"panel_domain": "control.example.com", "is_published": True},
            )
        self.assertIn("plain HTTP", str(caught.exception))

    def test_publishing_an_unknown_hostname_is_refused(self):
        with self.assertRaises(SecurityError) as caught:
            policy_update(
                policy=PanelAccessPolicy.load(),
                data={"panel_domain": "nowhere.example.com", "is_published": True},
            )
        self.assertIn("Add nowhere.example.com as a domain first", str(caught.exception))

    def test_requiring_client_certificates_needs_a_ca(self):
        """Without one Nginx refuses every connection, including the operator's."""
        with self.assertRaises(SecurityError):
            policy_update(policy=PanelAccessPolicy.load(), data={"require_mtls": True})

    def test_warnings_name_what_is_weak(self):
        domain = domain_create(name="control.example.com")
        certificate_generate_self_signed(domain=domain)

        policy = policy_update(
            policy=PanelAccessPolicy.load(),
            data={
                "panel_domain": "control.example.com",
                "is_published": True,
                "require_totp": False,
            },
        )

        warnings = " ".join(policy.exposure_warnings)
        self.assertIn("any address", warnings)
        self.assertIn("password is the only thing", warnings)


class PanelRenderTests(TestCase):
    """What the gateway actually serves for the panel hostname."""

    def setUp(self):
        self.domain = domain_create(name="control.example.com")
        certificate_generate_self_signed(domain=self.domain)

    def _publish(self, **extra):
        return policy_update(
            policy=PanelAccessPolicy.load(),
            data={
                "panel_domain": "control.example.com",
                "is_published": True,
                **extra,
            },
        )

    def test_nothing_is_rendered_until_it_is_published(self):
        config = config_render()["05-panel.conf"]
        self.assertNotIn("server {", config)

    def test_publishing_renders_a_tls_block_that_redirects_http(self):
        self._publish()
        config = config_render()["05-panel.conf"]

        self.assertIn("server_name control.example.com;", config)
        self.assertIn("listen 443 ssl;", config)
        self.assertIn("return 301 https://$host$request_uri;", config)

    def test_the_challenge_path_stays_on_plain_http(self):
        """Renewal happens while the panel is already on HTTPS."""
        self._publish()
        self.assertIn(
            "location ^~ /.well-known/acme-challenge/", config_render()["05-panel.conf"]
        )

    def test_the_allowlist_is_rendered_as_a_deny_by_default_rule(self):
        self._publish(ip_allowlist=["203.0.113.0/24"])
        config = config_render()["05-panel.conf"]

        self.assertIn("allow 203.0.113.0/24;", config)
        self.assertIn("deny all;", config)

    def test_client_certificates_are_verified_when_required(self):
        self._publish(require_mtls=True, mtls_ca_path="/etc/nginx/ssl/panel-ca.pem")
        config = config_render()["05-panel.conf"]

        self.assertIn("ssl_verify_client      on;", config)
        self.assertIn("ssl_client_certificate /etc/nginx/ssl/panel-ca.pem;", config)

    def test_the_django_admin_is_blocked_by_default(self):
        self._publish()
        config = config_render()["05-panel.conf"]

        self.assertIn("location /admin/ {", config)
        self.assertIn("return 404;", config)

    def test_sign_in_is_rate_limited_harder_than_everything_else(self):
        self._publish(login_rate_per_minute=6, api_rate_per_second=30)
        config = config_render()["05-panel.conf"]

        self.assertIn("zone=panel_login:10m rate=6r/m;", config)
        self.assertIn("zone=panel_api:10m   rate=30r/s;", config)
        self.assertIn("limit_req zone=panel_login burst=3 nodelay;", config)

    def test_security_headers_survive_locations_that_set_their_own(self):
        """
        Nginx replaces the whole inherited add_header set the moment a location
        declares one of its own. The location serving the panel's HTML sets a
        cache header, so without repeating them it would be the one page with
        no CSP at all.

        Asserted as the invariant rather than a count, so adding a location
        later cannot quietly satisfy it.
        """
        self._publish()
        config = config_render()["05-panel.conf"]

        for block in re.findall(r"\n    location [^{]*\{(.*?)\n    \}", config, re.DOTALL):
            if "add_header" not in block:
                continue  # inherits the server-level set untouched
            self.assertIn(
                "Content-Security-Policy", block,
                f"a location sets add_header but drops the security set:\n{block}",
            )
            self.assertIn("Strict-Transport-Security", block)

    def test_the_policy_loads_no_third_party_origin(self):
        self._publish()
        config = config_render()["05-panel.conf"]

        self.assertIn("default-src 'none'", config)
        self.assertNotIn("googleapis", config)
        self.assertNotIn("gstatic", config)

    def test_the_panel_hostname_is_not_also_rendered_as_a_proxied_domain(self):
        """Two server blocks for one name would make file order decide which wins."""
        from apps.backends.services import backend_create, instance_add
        from apps.routing.services import rule_create

        backend = backend_create(name="something")
        instance_add(backend=backend, address="10.0.1.11", port=8080)
        rule_create(domain_id=self.domain.id, backend_id=backend.id, match_value="/")

        self._publish()
        self.assertNotIn("control.example.com", config_render()["20-servers.conf"])

    def test_a_certificate_disappearing_keeps_the_panel_off_the_public_name(self):
        """
        The policy refuses to save without TLS, but a certificate can be deleted
        afterwards. Rendering checks again rather than trusting the stored flag.
        """
        self._publish()
        self.domain.ssl_enabled = False
        self.domain.ssl_cert_path = ""
        self.domain.save()

        self.assertNotIn("listen 443 ssl;", config_render()["05-panel.conf"])
