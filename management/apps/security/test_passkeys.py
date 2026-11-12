"""
Passkeys: where they are bound, and what the sign-in route still enforces.

The parts worth pinning are not the cryptography -- py_webauthn owns that --
but the decisions around it: which domain a credential is bound to, that an
unpublished panel refuses enrolment rather than binding to something
throwaway, and that signing in with a passkey passes through the same gates as
signing in with a password.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from apps.security.models import LoginAttempt, PanelAccessPolicy, Passkey
from apps.security.testing import make_operator
from apps.security.services import user_requires_totp_setup
from apps.security.passkeys import (
    LOGIN_CHALLENGES,
    REGISTER_CHALLENGES,
    PasskeysUnavailable,
    authentication_options,
    passkeys_available,
    registration_options,
    relying_party,
)

DOMAIN = "panel.example.com"


def publish(domain: str = DOMAIN) -> PanelAccessPolicy:
    policy = PanelAccessPolicy.load()
    policy.panel_domain = domain
    policy.is_published = True
    policy.save(update_fields=["panel_domain", "is_published"])
    return policy


class RelyingPartyTests(TestCase):
    """The binding decision, which cannot be changed after anyone enrols."""

    def test_rp_id_is_the_published_domain(self):
        publish()
        rp = relying_party()
        self.assertEqual(rp.id, DOMAIN)

    def test_origin_is_always_https(self):
        """A passkey ceremony needs a secure context, so no http origin is valid."""
        publish()
        self.assertEqual(relying_party().origin, f"https://{DOMAIN}")

    def test_unpublished_panel_refuses_rather_than_guessing(self):
        policy = PanelAccessPolicy.load()
        policy.is_published = False
        policy.save(update_fields=["is_published"])

        with self.assertRaises(PasskeysUnavailable):
            relying_party()
        self.assertFalse(passkeys_available())

    def test_published_without_a_domain_also_refuses(self):
        policy = PanelAccessPolicy.load()
        policy.is_published = True
        policy.panel_domain = ""
        policy.save(update_fields=["is_published", "panel_domain"])

        with self.assertRaises(PasskeysUnavailable):
            relying_party()


class CeremonyOptionsTests(TestCase):
    def setUp(self):
        publish()
        self.user = User.objects.create_user("operator", password="x" * 20)

    def test_registration_requires_user_verification(self):
        """This is what lets one passkey stand in for a password and a code."""
        session = {}
        options = registration_options(user=self.user, session=session)
        self.assertIn('"userVerification": "required"', options)

    def test_registration_asks_for_a_discoverable_credential(self):
        """Discoverable is what makes the sign-in need no username."""
        options = registration_options(user=self.user, session={})
        self.assertIn('"residentKey": "required"', options)

    def test_registration_stores_a_challenge_in_the_session(self):
        session = {}
        registration_options(user=self.user, session=session)
        self.assertEqual(len(session[REGISTER_CHALLENGES]), 1)

    def test_registration_excludes_passkeys_already_on_this_account(self):
        Passkey.objects.create(
            user=self.user, label="existing",
            credential_id="dGVzdC1jcmVkZW50aWFs", public_key="a2V5", rp_id=DOMAIN,
        )
        options = registration_options(user=self.user, session={})
        self.assertIn("dGVzdC1jcmVkZW50aWFs", options)

    def test_sign_in_options_name_no_credentials(self):
        """Listing them would tell an unauthenticated caller who is enrolled."""
        Passkey.objects.create(
            user=self.user, label="existing",
            credential_id="dGVzdC1jcmVkZW50aWFs", public_key="a2V5", rp_id=DOMAIN,
        )
        options = authentication_options(session={})
        self.assertNotIn("dGVzdC1jcmVkZW50aWFs", options)

    def test_each_ceremony_gets_a_fresh_challenge(self):
        first, second = {}, {}
        authentication_options(session=first)
        authentication_options(session=second)
        self.assertNotEqual(set(first[LOGIN_CHALLENGES]), set(second[LOGIN_CHALLENGES]))


class PasskeySignInGateTests(TestCase):
    """A second way in must not be a weaker way in."""

    def setUp(self):
        publish()
        self.user = User.objects.create_user("operator", password="x" * 20)
        self.passkey = Passkey.objects.create(
            user=self.user, label="laptop",
            credential_id="dGVzdC1jcmVkZW50aWFs", public_key="a2V5", rp_id=DOMAIN,
        )
        self.url = reverse("passkey-login-verify")

    def _verify_returns(self, passkey):
        return patch(
            "apps.accounts.apis.passkey_authentication_verify",
            return_value=passkey,
        )

    def test_a_disabled_account_cannot_sign_in_with_a_passkey(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        with self._verify_returns(self.passkey):
            response = self.client.post(
                self.url, {"credential": {"id": "x"}}, content_type="application/json"
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            LoginAttempt.objects.latest("created_at").outcome,
            LoginAttempt.Outcome.INACTIVE,
        )

    def test_a_successful_passkey_sign_in_is_recorded_as_one(self):
        with self._verify_returns(self.passkey):
            response = self.client.post(
                self.url, {"credential": {"id": "x"}}, content_type="application/json"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            LoginAttempt.objects.latest("created_at").outcome,
            LoginAttempt.Outcome.PASSKEY,
        )

    def test_a_passkey_sign_in_does_not_then_ask_for_a_code(self):
        """User verification already happened; a TOTP prompt would add a phishable step."""
        with self._verify_returns(self.passkey):
            response = self.client.post(
                self.url, {"credential": {"id": "x"}}, content_type="application/json"
            )

        self.assertFalse(response.json()["totp_setup_required"])

    def test_a_missing_credential_is_a_400_not_a_crash(self):
        response = self.client.post(self.url, {}, content_type="application/json")
        self.assertEqual(response.status_code, 400)


class SecondFactorTests(TestCase):
    """A passkey is a second factor, so it must not be held at the TOTP gate."""

    def setUp(self):
        publish()
        policy = PanelAccessPolicy.load()
        policy.require_totp = True
        policy.save(update_fields=["require_totp"])
        self.user = User.objects.create_user("operator", password="x" * 20)

    def test_an_account_with_no_factor_is_held(self):
        self.assertTrue(user_requires_totp_setup(user=self.user))

    def test_a_passkey_satisfies_the_requirement(self):
        Passkey.objects.create(
            user=self.user, label="laptop",
            credential_id="cGFzc2tleQ", public_key="a2V5", rp_id=DOMAIN,
        )
        self.assertFalse(user_requires_totp_setup(user=self.user))

    def test_a_passkey_holder_can_use_the_panel(self):
        """Without this the operator signs in and is then refused every request."""
        Passkey.objects.create(
            user=self.user, label="laptop",
            credential_id="cGFzc2tleQ", public_key="a2V5", rp_id=DOMAIN,
        )
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save(update_fields=["is_staff", "is_superuser"])

        self.client.force_login(self.user)
        response = self.client.get(reverse("security-passkeys"))
        self.assertEqual(response.status_code, 200)


class PasskeyOwnershipTests(TestCase):
    """One operator must not be able to remove another's credential."""

    def setUp(self):
        publish()
        self.owner = make_operator(username="owner")
        self.other = make_operator(username="other")
        self.passkey = Passkey.objects.create(
            user=self.owner, label="owner key",
            credential_id="b3duZXI", public_key="a2V5", rp_id=DOMAIN,
        )

    def test_another_operator_cannot_delete_it(self):
        self.client.force_login(self.other)
        response = self.client.delete(
            reverse("security-passkey-detail", args=[self.passkey.pk])
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Passkey.objects.filter(pk=self.passkey.pk).exists())

    def test_the_owner_can(self):
        self.client.force_login(self.owner)
        response = self.client.delete(
            reverse("security-passkey-detail", args=[self.passkey.pk])
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Passkey.objects.filter(pk=self.passkey.pk).exists())
