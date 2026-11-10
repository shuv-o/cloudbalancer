"""
Adding an operator, and the first password that account has to replace.

Onboarding was a shell command before this: undocumented, unaudited, and
reached for by whoever had SSH rather than whoever had admin. Moving it into
the panel means the rules that already apply to everything else -- who may do
it, what gets recorded, what the new account must do first -- apply to it too.

The forced password change exists because the admin who creates an account
chooses its first password and therefore knows it. Until the owner replaces
it, the audit trail cannot honestly say which of the two of them acted.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from apps.security.models import AuditEvent, OperatorProfile
from apps.security.roles import Role, role_of
from apps.security.services import SecurityError, operator_create, operator_update
from apps.security.testing import make_operator

GOOD_PASSWORD = "unrelated-passphrase-91"


class OperatorCreateTests(TestCase):
    def setUp(self):
        self.admin = make_operator(username="boss", superuser=True)
        self.client.force_login(self.admin)

    def create(self, **overrides):
        body = {
            "username": "rahim",
            "email": "rahim@example.org",
            "password": GOOD_PASSWORD,
            "role": Role.VIEWER,
        }
        body.update(overrides)
        return self.client.post(
            "/api/v1/security/operators/", body, content_type="application/json"
        )

    def test_an_admin_can_add_an_account(self):
        response = self.create()
        self.assertEqual(response.status_code, 201, response.content)

        user = User.objects.get(username="rahim")
        self.assertEqual(role_of(user), Role.VIEWER)
        self.assertFalse(user.is_superuser)

    def test_a_new_account_is_visible_on_the_operators_page(self):
        """
        is_staff is what that page filters on.

        An account without it works but appears nowhere -- including in the
        count of operators lacking a second factor, which is the number meant
        to catch exactly this.
        """
        self.create()
        self.assertTrue(User.objects.get(username="rahim").is_staff)

        listed = self.client.get("/api/v1/security/operators/").json()["operators"]
        self.assertIn("rahim", [o["username"] for o in listed])

    def test_the_first_password_must_be_replaced(self):
        self.create()
        profile = OperatorProfile.objects.get(user__username="rahim")
        self.assertTrue(profile.password_change_required)

    def test_creating_an_account_is_recorded(self):
        self.create()
        self.assertTrue(
            AuditEvent.objects.filter(
                path="/api/v1/security/operators/", action="POST"
            ).exists()
        )

    def test_a_duplicate_username_is_refused(self):
        self.create()
        response = self.create()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(User.objects.filter(username="rahim").count(), 1)

    def test_a_weak_password_is_refused_by_django_s_own_validators(self):
        response = self.create(password="password")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(User.objects.filter(username="rahim").exists())

    def test_an_unknown_role_is_refused(self):
        self.assertEqual(self.create(role="superadmin").status_code, 400)

    def test_the_response_does_not_echo_the_password(self):
        body = self.create().json()
        self.assertNotIn(GOOD_PASSWORD, str(body))

    def test_a_non_admin_cannot_add_an_account(self):
        self.client.force_login(
            make_operator(username="op", superuser=False, role=Role.OPERATOR)
        )
        self.assertEqual(self.create().status_code, 403)
        self.assertFalse(User.objects.filter(username="rahim").exists())


class OperatorUpdateTests(TestCase):
    def setUp(self):
        self.admin = make_operator(username="boss", superuser=True)
        self.target = make_operator(
            username="rahim", superuser=False, role=Role.VIEWER
        )
        self.client.force_login(self.admin)

    def patch(self, body, user_id=None):
        return self.client.patch(
            f"/api/v1/security/operators/{user_id or self.target.pk}/",
            body,
            content_type="application/json",
        )

    def test_an_admin_can_promote_someone(self):
        self.assertEqual(self.patch({"role": Role.OPERATOR}).status_code, 200)
        self.target.refresh_from_db()
        self.assertEqual(role_of(self.target), Role.OPERATOR)

    def test_an_admin_can_switch_an_account_off(self):
        self.assertEqual(self.patch({"is_active": False}).status_code, 200)
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)

    def test_switching_someone_off_ends_their_session(self):
        """
        Otherwise they keep working until the session expires.

        Which is not what switching an account off is understood to mean, and
        is the gap that matters when someone is being removed in a hurry.
        """
        theirs = self.client_class()
        theirs.force_login(self.target)
        self.assertEqual(theirs.get("/api/v1/domains/").status_code, 200)

        self.patch({"is_active": False})
        self.assertFalse(theirs.get("/api/v1/auth/me/").json()["authenticated"])

    def test_an_admin_cannot_demote_themselves(self):
        """
        There is no undo, and no second chance to notice.

        An admin who removes their own admin cannot restore it, and one who
        deactivates themselves is locked out the same instant -- both needing
        the shell to recover, which is the thing this page exists to avoid.
        """
        other = make_operator(username="other", superuser=False, role=Role.ADMIN)
        self.client.force_login(other)
        response = self.patch({"role": Role.VIEWER}, user_id=other.pk)
        self.assertEqual(response.status_code, 400)
        other.refresh_from_db()
        self.assertEqual(role_of(other), Role.ADMIN)

    def test_a_superusers_role_cannot_be_changed_from_the_panel(self):
        root = make_operator(username="root", superuser=True)
        response = self.patch({"role": Role.VIEWER}, user_id=root.pk)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(role_of(root), Role.ADMIN)

    def test_an_empty_patch_is_refused_rather_than_silently_doing_nothing(self):
        self.assertEqual(self.patch({}).status_code, 400)

    def test_an_unknown_account_is_a_404(self):
        self.assertEqual(self.patch({"role": Role.VIEWER}, user_id=99999).status_code, 404)


class ForcedPasswordChangeTests(TestCase):
    def setUp(self):
        self.user = make_operator(
            username="rahim",
            password="chosen-by-the-admin-77",
            superuser=False,
            role=Role.OPERATOR,
            password_change_required=True,
        )
        self.client.force_login(self.user)

    def test_the_panel_is_closed_until_the_password_changes(self):
        response = self.client.get("/api/v1/domains/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["password_change_required"])

    def test_a_passkey_cannot_be_enrolled_first(self):
        """
        A passkey enrolled now would outlive the password it was created under.

        The admin knows that password. A credential minted with it would sign
        in on its own afterwards, which makes the forced change ceremonial.
        """
        response = self.client.post("/api/v1/auth/passkey/register/options/")
        self.assertEqual(response.status_code, 403)

    def test_changing_the_password_opens_the_panel(self):
        response = self.client.post(
            "/api/v1/security/password/",
            {
                "current_password": "chosen-by-the-admin-77",
                "new_password": "something-only-rahim-knows-4",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)

        self.assertFalse(
            OperatorProfile.objects.get(user=self.user).password_change_required
        )
        self.assertEqual(self.client.get("/api/v1/domains/").status_code, 200)

    def test_the_change_does_not_sign_the_person_out(self):
        """
        Django rotates the session auth hash on a password change.

        Without update_session_auth_hash() in the view, a successful change
        ends the session that made it -- so the person is thrown back to the
        sign-in screen by having done exactly what they were told to do.
        """
        self.client.post(
            "/api/v1/security/password/",
            {
                "current_password": "chosen-by-the-admin-77",
                "new_password": "something-only-rahim-knows-4",
            },
            content_type="application/json",
        )
        self.assertTrue(self.client.get("/api/v1/auth/me/").json()["authenticated"])

    def test_the_wrong_current_password_is_refused(self):
        response = self.client.post(
            "/api/v1/security/password/",
            {"current_password": "guess", "new_password": "another-good-one-12"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            OperatorProfile.objects.get(user=self.user).password_change_required
        )

    def test_reusing_the_same_password_is_refused(self):
        response = self.client.post(
            "/api/v1/security/password/",
            {
                "current_password": "chosen-by-the-admin-77",
                "new_password": "chosen-by-the-admin-77",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class PasswordChangeTests(TestCase):
    """Available to every role, not only to accounts being forced."""

    def test_a_viewer_can_change_their_own_password(self):
        user = make_operator(
            username="viewer",
            password="the-old-one-55",
            superuser=False,
            role=Role.VIEWER,
        )
        self.client.force_login(user)
        response = self.client.post(
            "/api/v1/security/password/",
            {"current_password": "the-old-one-55", "new_password": "the-new-one-66"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        user.refresh_from_db()
        self.assertTrue(user.check_password("the-new-one-66"))

    def test_other_sessions_end(self):
        """
        Someone changing a password they think is compromised means it.

        Django derives the session auth hash from the password, so every other
        session stops validating. This pins that, because the day it stops
        being true is the day the change becomes decorative.
        """
        user = make_operator(
            username="viewer",
            password="the-old-one-55",
            superuser=False,
            role=Role.VIEWER,
        )
        elsewhere = self.client_class()
        elsewhere.force_login(user)
        self.assertTrue(elsewhere.get("/api/v1/auth/me/").json()["authenticated"])

        self.client.force_login(user)
        self.client.post(
            "/api/v1/security/password/",
            {"current_password": "the-old-one-55", "new_password": "the-new-one-66"},
            content_type="application/json",
        )

        self.assertFalse(elsewhere.get("/api/v1/auth/me/").json()["authenticated"])


class ServiceGuardTests(TestCase):
    """The services refuse too, not only the views in front of them."""

    def test_operator_create_refuses_a_non_admin(self):
        actor = make_operator(username="op", superuser=False, role=Role.OPERATOR)
        with self.assertRaises(SecurityError):
            operator_create(
                actor=actor,
                username="x",
                email="",
                password=GOOD_PASSWORD,
                role=Role.VIEWER,
            )

    def test_operator_update_refuses_a_non_admin(self):
        actor = make_operator(username="op", superuser=False, role=Role.OPERATOR)
        target = make_operator(username="t", superuser=False, role=Role.VIEWER)
        with self.assertRaises(SecurityError):
            operator_update(actor=actor, target_user=target, role=Role.ADMIN)


class BackfillTests(TestCase):
    """
    The migration that decides whether anyone can still sign in after upgrade.

    An account with no profile holds no role and is refused everything. So a
    backfill that misses an account does not degrade it -- it locks the person
    out, and on an installation whose accounts are all non-superusers it locks
    out everyone at once. This calls the migration's own function against the
    real models rather than restating what it should do.
    """

    def test_existing_non_superusers_become_operators(self):
        from django.apps import apps as django_apps

        from apps.security.backfills import (
            give_every_existing_account_the_role_it_already_had,
        )

        legacy = User.objects.create_user(username="legacy", password="x")
        root = User.objects.create_superuser(username="root", password="x")
        OperatorProfile.objects.all().delete()

        give_every_existing_account_the_role_it_already_had(django_apps, None)

        legacy.refresh_from_db()
        self.assertEqual(role_of(legacy), Role.OPERATOR)

        # A superuser needs no row: role_of answers from the Django flag.
        self.assertFalse(OperatorProfile.objects.filter(user=root).exists())
        self.assertEqual(role_of(root), Role.ADMIN)

    def test_running_it_twice_changes_nothing(self):
        """Migrations get re-run by hand more often than anyone admits."""
        from django.apps import apps as django_apps

        from apps.security.backfills import (
            give_every_existing_account_the_role_it_already_had,
        )

        User.objects.create_user(username="legacy", password="x")
        OperatorProfile.objects.all().delete()

        give_every_existing_account_the_role_it_already_had(django_apps, None)
        give_every_existing_account_the_role_it_already_had(django_apps, None)

        self.assertEqual(OperatorProfile.objects.filter(user__username="legacy").count(), 1)
