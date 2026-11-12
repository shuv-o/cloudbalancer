"""
Passkeys under the conditions that break them in practice.

test_passkeys.py pins the design. This file pins the failure modes: several
tabs, a page left open, a double-submitted form, two assertions racing, a
panel that moved hostname, a lost device -- and the one that was a hole
rather than an inconvenience, a password standing in for a required second
factor because the account also held a passkey.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.security.backfills import bind_existing_passkeys_to_the_current_domain
from apps.security.models import PanelAccessPolicy, Passkey, TotpDevice
from apps.security.passkeys import (
    CHALLENGE_TTL_SECONDS,
    LOGIN_CHALLENGES,
    MAX_OUTSTANDING_CHALLENGES,
    REGISTER_CHALLENGES,
    PasskeyError,
    authentication_options,
    authentication_verify,
    registration_options,
    registration_verify,
    usable_passkeys,
)
from apps.security.services import totp_reset_for_user, user_requires_totp_setup
from apps.security.testing import make_operator
from webauthn.helpers import bytes_to_base64url

DOMAIN = "panel.example.com"
PASSWORD = "correct horse battery staple"


def publish(domain: str = DOMAIN) -> PanelAccessPolicy:
    policy = PanelAccessPolicy.load()
    policy.panel_domain = domain
    policy.is_published = True
    policy.save(update_fields=["panel_domain", "is_published"])
    return policy


def require_second_factor(on: bool = True) -> None:
    policy = PanelAccessPolicy.load()
    policy.require_totp = on
    policy.save(update_fields=["require_totp"])


def answering(challenge: str, credential_id: str = "Y3JlZA") -> dict:
    """A browser response that names `challenge` the way a real one does."""
    client_data = json.dumps({"type": "webauthn.create", "challenge": challenge})
    return {
        "id": credential_id,
        "rawId": credential_id,
        "type": "public-key",
        "transports": ["internal", "<script>"],
        "response": {"clientDataJSON": bytes_to_base64url(client_data.encode())},
    }


def registered(credential_id: bytes = b"cred"):
    """What py_webauthn returns for a registration it accepted."""
    return patch(
        "apps.security.passkeys.verify_registration_response",
        return_value=SimpleNamespace(
            credential_id=credential_id,
            credential_public_key=b"key",
            sign_count=0,
            credential_backed_up=True,
        ),
    )


def asserted(new_sign_count: int = 1):
    return patch(
        "apps.security.passkeys.verify_authentication_response",
        return_value=SimpleNamespace(new_sign_count=new_sign_count),
    )


class ChallengeStoreTests(TestCase):
    """Challenges that survive tabs and refreshes, and nothing else."""

    def setUp(self):
        publish()
        self.user = User.objects.create_user("operator", password=PASSWORD)

    def _issue(self, session):
        registration_options(user=self.user, session=session)
        return list(session[REGISTER_CHALLENGES])[-1]

    def test_two_tabs_can_both_enrol(self):
        """The single-slot store failed whichever of these finished second."""
        session = {}
        first = self._issue(session)
        second = self._issue(session)

        with registered(b"one"):
            registration_verify(user=self.user, session=session,
                                credential=answering(first), label="a")
        with registered(b"two"):
            registration_verify(user=self.user, session=session,
                                credential=answering(second), label="b")

        self.assertEqual(Passkey.objects.filter(user=self.user).count(), 2)

    def test_signing_in_elsewhere_does_not_spend_an_enrolment(self):
        session = {}
        challenge = self._issue(session)
        authentication_options(session=session)

        with registered():
            registration_verify(user=self.user, session=session,
                                credential=answering(challenge), label="a")

    def test_a_challenge_is_redeemable_once(self):
        session = {}
        challenge = self._issue(session)

        with registered():
            registration_verify(user=self.user, session=session,
                                credential=answering(challenge), label="a")
        with registered(b"again"), self.assertRaises(PasskeyError):
            registration_verify(user=self.user, session=session,
                                credential=answering(challenge), label="a")

    def test_a_challenge_expires(self):
        session = {}
        challenge = self._issue(session)
        entry = session[REGISTER_CHALLENGES][challenge]
        entry["at"] -= CHALLENGE_TTL_SECONDS + 1

        with registered(), self.assertRaisesMessage(PasskeyError, "expired"):
            registration_verify(user=self.user, session=session,
                                credential=answering(challenge), label="a")

    def test_a_challenge_never_issued_is_refused(self):
        with registered(), self.assertRaises(PasskeyError):
            registration_verify(user=self.user, session={},
                                credential=answering("bm90LWlzc3VlZA"), label="a")

    def test_an_enrolment_is_bound_to_the_account_that_started_it(self):
        session = {}
        challenge = self._issue(session)
        other = User.objects.create_user("other", password=PASSWORD)

        with registered(), self.assertRaises(PasskeyError):
            registration_verify(user=other, session=session,
                                credential=answering(challenge), label="a")

    def test_an_unreadable_response_is_a_clean_refusal(self):
        session = {}
        self._issue(session)
        with self.assertRaises(PasskeyError):
            registration_verify(user=self.user, session=session,
                                credential={"response": {"clientDataJSON": "!!"}},
                                label="a")

    def test_the_store_is_bounded(self):
        session = {}
        for _ in range(MAX_OUTSTANDING_CHALLENGES + 5):
            authentication_options(session=session)
        self.assertEqual(len(session[LOGIN_CHALLENGES]), MAX_OUTSTANDING_CHALLENGES)


class EnrolmentTests(TestCase):
    def setUp(self):
        publish()
        self.user = User.objects.create_user("operator", password=PASSWORD)

    def _enrol(self, credential_id: bytes = b"cred", label: str = "laptop"):
        session = {}
        registration_options(user=self.user, session=session)
        challenge = list(session[REGISTER_CHALLENGES])[-1]
        with registered(credential_id):
            return registration_verify(user=self.user, session=session,
                                       credential=answering(challenge), label=label)

    def test_the_domain_is_recorded(self):
        self.assertEqual(self._enrol().rp_id, DOMAIN)

    def test_a_duplicate_is_a_refusal_not_a_500(self):
        self._enrol(b"same")
        with self.assertRaisesMessage(PasskeyError, "already registered"):
            self._enrol(b"same")
        # And the connection is still usable, which an unhandled
        # IntegrityError inside the request's transaction would not leave it.
        self.assertEqual(Passkey.objects.count(), 1)

    def test_transports_are_kept_to_known_values(self):
        self.assertEqual(self._enrol().transports, ["internal"])

    def test_the_library_reason_is_logged_not_returned(self):
        session = {}
        registration_options(user=self.user, session=session)
        challenge = list(session[REGISTER_CHALLENGES])[-1]
        with patch("apps.security.passkeys.verify_registration_response",
                   side_effect=Exception("Unexpected RP ID hash")):
            with self.assertLogs("apps.security.passkeys", "WARNING"):
                with self.assertRaises(PasskeyError) as caught:
                    registration_verify(user=self.user, session=session,
                                        credential=answering(challenge), label="x")
        self.assertNotIn("hash", str(caught.exception))


class SignInTests(TestCase):
    def setUp(self):
        publish()
        self.user = User.objects.create_user("operator", password=PASSWORD)
        self.passkey = Passkey.objects.create(
            user=self.user, label="laptop", credential_id="Y3JlZA",
            public_key="a2V5", rp_id=DOMAIN, sign_count=5,
        )

    def _challenge(self, session):
        authentication_options(session=session)
        return list(session[LOGIN_CHALLENGES])[-1]

    def test_the_counter_advances(self):
        session = {}
        with asserted(6):
            authentication_verify(session=session,
                                  credential=answering(self._challenge(session)))
        self.passkey.refresh_from_db()
        self.assertEqual(self.passkey.sign_count, 6)
        self.assertIsNotNone(self.passkey.last_used_at)

    def test_two_racing_assertions_cannot_both_succeed(self):
        """The second was verified against a count the first already moved."""
        session = {}
        first, second = self._challenge(session), self._challenge(session)

        with asserted(6):
            authentication_verify(session=session, credential=answering(first))

        stale = Passkey.objects.get(pk=self.passkey.pk)
        stale.sign_count = 5  # what the second request read before the first wrote
        with asserted(6), patch(
            "apps.security.passkeys.Passkey.objects.select_related",
        ) as select:
            select.return_value.filter.return_value.first.return_value = stale
            with self.assertRaises(PasskeyError):
                authentication_verify(session=session, credential=answering(second))

    def test_a_passkey_from_an_earlier_hostname_is_refused_with_a_reason(self):
        publish("new.example.com")
        session = {}
        with asserted(), self.assertRaisesMessage(PasskeyError, DOMAIN):
            authentication_verify(session=session,
                                  credential=answering(self._challenge(session)))


class PasswordIsNotASecondFactorTests(TestCase):
    """
    The hole. With a second factor required, an account whose only factor was
    a passkey could sign in with its password alone, and the enrolment gate
    then let it through because the passkey counted.
    """

    def setUp(self):
        publish()
        require_second_factor()
        self.user = make_operator(username="operator", password=PASSWORD,
                                  superuser=False, with_second_factor=False)
        self.url = reverse("auth-login")

    def _passkey(self, rp_id=DOMAIN):
        return Passkey.objects.create(
            user=self.user, label="laptop", credential_id="Y3JlZA",
            public_key="a2V5", rp_id=rp_id,
        )

    def _sign_in(self):
        return self.client.post(
            self.url, {"username": "operator", "password": PASSWORD},
            content_type="application/json",
        )

    def test_a_password_alone_does_not_open_a_passkey_account(self):
        self._passkey()
        response = self._sign_in()

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.json()["passkey_required"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_without_the_requirement_a_password_is_enough(self):
        require_second_factor(False)
        self._passkey()
        self.assertEqual(self._sign_in().status_code, 200)

    def test_an_authenticator_app_still_works_beside_a_passkey(self):
        self._passkey()
        TotpDevice.objects.create(user=self.user, secret="JBSWY3DPEHPK3PXP",
                                  confirmed_at=timezone.now(), recovery_codes=[])
        response = self._sign_in()
        self.assertTrue(response.json().get("totp_required"))

    def test_a_passkey_that_cannot_work_here_does_not_lock_the_account_out(self):
        """Moved hostname: sign in with the password, then be asked to enrol."""
        self._passkey(rp_id="old.example.com")
        response = self._sign_in()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["totp_setup_required"])


class MovedHostnameTests(TestCase):
    def setUp(self):
        publish()
        self.user = make_operator(username="operator", password=PASSWORD,
                                  superuser=False, with_second_factor=False)
        Passkey.objects.create(user=self.user, label="old", credential_id="b2xk",
                               public_key="a2V5", rp_id="old.example.com")
        Passkey.objects.create(user=self.user, label="new", credential_id="bmV3",
                               public_key="a2V5", rp_id=DOMAIN)

    def test_only_passkeys_for_this_address_are_usable(self):
        self.assertEqual([k.label for k in usable_passkeys(self.user)], ["new"])

    def test_an_unpublished_panel_has_no_usable_passkeys(self):
        policy = PanelAccessPolicy.load()
        policy.is_published = False
        policy.save(update_fields=["is_published"])
        self.assertFalse(usable_passkeys(self.user).exists())

    def test_a_stale_passkey_does_not_satisfy_the_requirement(self):
        require_second_factor()
        Passkey.objects.filter(rp_id=DOMAIN).delete()
        self.assertTrue(user_requires_totp_setup(user=self.user))

    def test_the_list_says_which_ones_work(self):
        self.client.force_login(self.user)
        body = self.client.get(reverse("security-passkeys")).json()

        self.assertEqual(body["rp_id"], DOMAIN)
        self.assertEqual(
            {k["label"]: k["usable"] for k in body["passkeys"]},
            {"old": False, "new": True},
        )


class LostDeviceTests(TestCase):
    """The break-glass reset has to reset every factor, or it resets nothing."""

    def setUp(self):
        publish()
        require_second_factor()
        self.admin = User.objects.create_superuser("admin", password=PASSWORD)
        self.user = make_operator(username="operator", password=PASSWORD,
                                  superuser=False, with_second_factor=False)
        Passkey.objects.create(user=self.user, label="phone", credential_id="cGhvbmU",
                               public_key="a2V5", rp_id=DOMAIN)
        TotpDevice.objects.create(user=self.user, secret="JBSWY3DPEHPK3PXP",
                                  confirmed_at=timezone.now(), recovery_codes=[])

    def test_reset_clears_passkeys_as_well(self):
        totp_reset_for_user(actor=self.admin, target_user=self.user)

        self.assertFalse(Passkey.objects.filter(user=self.user).exists())
        self.assertFalse(TotpDevice.objects.filter(user=self.user).exists())
        self.assertTrue(user_requires_totp_setup(user=self.user))

    def test_after_reset_the_password_gets_them_to_enrolment(self):
        totp_reset_for_user(actor=self.admin, target_user=self.user)
        response = self.client.post(
            reverse("auth-login"), {"username": "operator", "password": PASSWORD},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["totp_setup_required"])


class BackfillTests(TestCase):
    """Calls the function the migration runs, not a restatement of it."""

    def setUp(self):
        self.user = User.objects.create_user("operator", password=PASSWORD)
        self.key = Passkey.objects.create(user=self.user, label="k",
                                          credential_id="aw", public_key="a2V5")

    def _run(self):
        from django.apps import apps
        bind_existing_passkeys_to_the_current_domain(apps, None)
        self.key.refresh_from_db()

    def test_existing_passkeys_are_bound_to_the_published_domain(self):
        publish()
        self._run()
        self.assertEqual(self.key.rp_id, DOMAIN)

    def test_an_unpublished_panel_leaves_them_unbound(self):
        self._run()
        self.assertEqual(self.key.rp_id, "")

    def test_it_does_not_rebind_a_passkey_that_already_has_a_domain(self):
        publish()
        Passkey.objects.filter(pk=self.key.pk).update(rp_id="old.example.com")
        self._run()
        self.assertEqual(self.key.rp_id, "old.example.com")


class MigrationTests(TransactionTestCase):
    """The upgrade path itself, from the schema before rp_id existed."""

    before = [("security", "0004_operatorprofile")]
    after = [("security", "0005_passkey_rp_id")]

    def test_upgrading_binds_existing_passkeys(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        old = executor.loader.project_state(self.before).apps

        old.get_model("security", "PanelAccessPolicy").objects.update_or_create(
            pk=1, defaults={"panel_domain": DOMAIN, "is_published": True},
        )
        user = old.get_model("auth", "User").objects.create(username="u")
        old.get_model("security", "Passkey").objects.create(
            user_id=user.pk, label="k", credential_id="aw", public_key="a2V5",
        )

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(self.after)
        new = executor.loader.project_state(self.after).apps

        self.assertEqual(
            new.get_model("security", "Passkey").objects.get().rp_id, DOMAIN
        )

        # Leave the schema where the rest of the suite expects it.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())


class OperatorListTests(TestCase):
    """What an admin sees decides whether they can rescue someone."""

    def setUp(self):
        publish()
        self.user = make_operator(username="operator", password=PASSWORD,
                                  superuser=False, with_second_factor=False)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])

    def _row(self):
        from apps.security.selectors import operator_list
        return next(r for r in operator_list() if r["id"] == self.user.pk)

    def test_a_passkey_only_operator_has_a_second_factor(self):
        Passkey.objects.create(user=self.user, label="k", credential_id="aw",
                               public_key="a2V5", rp_id=DOMAIN)
        row = self._row()
        self.assertTrue(row["has_second_factor"])
        self.assertFalse(row["has_authenticator"])
        self.assertEqual(row["passkey_count"], 1)

    def test_a_stale_passkey_is_not_a_second_factor_but_is_still_resettable(self):
        Passkey.objects.create(user=self.user, label="k", credential_id="aw",
                               public_key="a2V5", rp_id="old.example.com")
        row = self._row()
        self.assertFalse(row["has_second_factor"])
        self.assertEqual(row["passkey_count"], 1)

    def test_the_summary_counts_passkey_holders_as_protected(self):
        from apps.security.selectors import security_summary
        Passkey.objects.create(user=self.user, label="k", credential_id="aw",
                               public_key="a2V5", rp_id=DOMAIN)
        operators = security_summary()["operators"]
        self.assertEqual(operators["without_second_factor"], 0)
