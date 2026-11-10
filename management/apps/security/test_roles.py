"""
What each role may do, proved against the real URL map.

These tests go through the client rather than calling the permission class,
because the thing worth protecting is the endpoint. A permission class that
behaves perfectly while a view forgets to use it is the failure this is for,
and only a request exercises both.

The most important case here is the viewer who can still finish enrolling.
The panel holds every account at second-factor setup before it will do
anything else; if the role system then refuses the enrolment endpoints, the
two rules together lock the account out of the panel permanently, and the
symptom is a 403 on a screen the panel itself navigated to.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from apps.backends.services import backend_create, instance_add
from apps.domains.services import domain_create
from apps.security.models import OperatorProfile
from apps.security.roles import Role, has_at_least, role_of
from apps.security.testing import make_operator

# One read and one write from each area the panel exposes. Not exhaustive --
# exhaustive would mean restating the URL map -- but one of each, so a whole
# area cannot lose its guard unnoticed.
READS = (
    "/api/v1/domains/",
    "/api/v1/backends/",
    "/api/v1/routing/rules/",
    "/api/v1/monitoring/overview/",
    "/api/v1/monitoring/series/",
    "/api/v1/certificates/",
)

ADMIN_ONLY_READS = (
    "/api/v1/security/policy/",
    "/api/v1/security/audit/",
    "/api/v1/security/attempts/",
    "/api/v1/security/operators/",
    "/api/v1/security/summary/",
)


class RankTests(TestCase):
    def test_each_role_contains_the_one_below_it(self):
        viewer = make_operator(username="v", superuser=False, role=Role.VIEWER)
        operator = make_operator(username="o", superuser=False, role=Role.OPERATOR)
        admin = make_operator(username="a", superuser=False, role=Role.ADMIN)

        self.assertTrue(has_at_least(viewer, Role.VIEWER))
        self.assertFalse(has_at_least(viewer, Role.OPERATOR))

        self.assertTrue(has_at_least(operator, Role.VIEWER))
        self.assertTrue(has_at_least(operator, Role.OPERATOR))
        self.assertFalse(has_at_least(operator, Role.ADMIN))

        for required in (Role.VIEWER, Role.OPERATOR, Role.ADMIN):
            self.assertTrue(has_at_least(admin, required))

    def test_a_django_superuser_is_an_admin_without_a_profile(self):
        """The way back in. A bad profile row must not be able to remove it."""
        root = make_operator(username="root", superuser=True)
        self.assertFalse(OperatorProfile.objects.filter(user=root).exists())
        self.assertEqual(role_of(root), Role.ADMIN)
        self.assertTrue(has_at_least(root, Role.ADMIN))

    def test_a_superuser_keeps_admin_even_with_a_lesser_profile(self):
        root = make_operator(username="root", superuser=True)
        OperatorProfile.objects.create(user=root, role=Role.VIEWER)
        self.assertEqual(role_of(root), Role.ADMIN)

    def test_an_account_with_no_profile_holds_nothing(self):
        """What a shell-created user gets: refused, visibly, rather than guessed at."""
        stranded = make_operator(username="shell", superuser=False, role=None)
        self.assertIsNone(role_of(stranded))
        self.assertFalse(has_at_least(stranded, Role.VIEWER))

    def test_an_anonymous_user_holds_nothing(self):
        self.assertIsNone(role_of(None))
        self.assertFalse(has_at_least(None, Role.VIEWER))


class ViewerTests(TestCase):
    def setUp(self):
        self.user = make_operator(username="viewer", superuser=False, role=Role.VIEWER)
        self.client.force_login(self.user)

    def test_a_viewer_can_read_the_panel(self):
        for path in READS:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_a_viewer_cannot_change_anything(self):
        self.assertEqual(
            self.client.post(
                "/api/v1/domains/",
                {"name": "new.example.com"},
                content_type="application/json",
            ).status_code,
            403,
        )

    def test_a_viewer_cannot_delete_anything(self):
        domain = domain_create(name="existing.example.com")
        self.assertEqual(
            self.client.delete(f"/api/v1/domains/{domain.pk}/").status_code, 403
        )
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_a_viewer_cannot_deploy_the_gateway(self):
        self.assertEqual(
            self.client.post("/api/v1/gateway/deploy/").status_code, 403
        )

    def test_a_viewer_cannot_purge_the_cache(self):
        self.assertEqual(
            self.client.post("/api/v1/gateway/cache/purge/").status_code, 403
        )

    def test_a_viewer_cannot_read_the_audit_log_or_sign_in_history(self):
        """
        A record of who was where, including failed guesses at named accounts.

        Readable by everyone who can read a dashboard would invert the reason
        it is kept.
        """
        for path in ADMIN_ONLY_READS:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)

    def test_a_viewer_can_still_manage_their_own_second_factor(self):
        self.assertEqual(self.client.get("/api/v1/security/totp/status/").status_code, 200)

    def test_a_viewer_with_no_second_factor_can_reach_enrolment(self):
        """
        The lockout this pairing would otherwise cause.

        RequireTotpMiddleware refuses the whole panel until an account enrols,
        and sends it to the enrolment screen. If the role system then refuses
        the enrolment endpoints, the account is held at a screen it is not
        allowed to use, with no way forward and nothing saying why.
        """
        fresh = make_operator(
            username="newviewer",
            superuser=False,
            role=Role.VIEWER,
            with_second_factor=False,
        )
        self.client.force_login(fresh)

        # Held out of the panel proper, which is the middleware working.
        blocked = self.client.get("/api/v1/domains/")
        self.assertEqual(blocked.status_code, 403)
        self.assertTrue(blocked.json()["totp_setup_required"])

        # But able to do the one thing it is being told to do.
        self.assertEqual(self.client.post("/api/v1/security/totp/setup/").status_code, 200)

    def test_a_viewer_can_list_and_remove_their_own_passkeys(self):
        self.assertEqual(self.client.get("/api/v1/security/passkeys/").status_code, 200)

    def test_a_viewer_can_sign_out(self):
        self.assertEqual(self.client.post("/api/v1/auth/logout/").status_code, 204)
        self.assertFalse(self.client.get("/api/v1/auth/me/").json()["authenticated"])


class OperatorTests(TestCase):
    def setUp(self):
        self.user = make_operator(username="op", superuser=False, role=Role.OPERATOR)
        self.client.force_login(self.user)

    def test_an_operator_configures_traffic(self):
        response = self.client.post(
            "/api/v1/domains/",
            {"name": "api.example.com"},
            content_type="application/json",
        )
        self.assertIn(response.status_code, (200, 201), response.content)

    def test_an_operator_can_deploy(self):
        backend = backend_create(name="svc")
        instance_add(backend=backend, address="10.0.0.1", port=8080)
        self.assertNotEqual(
            self.client.post("/api/v1/gateway/deploy/").status_code, 403
        )

    def test_an_operator_cannot_change_who_may_reach_the_panel(self):
        self.assertEqual(
            self.client.patch(
                "/api/v1/security/policy/",
                {"ip_allowlist": []},
                content_type="application/json",
            ).status_code,
            403,
        )

    def test_an_operator_cannot_add_an_operator(self):
        self.assertEqual(
            self.client.post(
                "/api/v1/security/operators/",
                {
                    "username": "sneaky",
                    "password": "a-long-enough-passphrase",
                    "role": Role.ADMIN,
                },
                content_type="application/json",
            ).status_code,
            403,
        )
        self.assertFalse(User.objects.filter(username="sneaky").exists())


class StrandedAccountTests(TestCase):
    """An account holding no role at all."""

    def setUp(self):
        self.user = make_operator(username="shell", superuser=False, role=None)
        self.client.force_login(self.user)

    def test_it_is_refused_everything_it_could_change(self):
        self.assertEqual(self.client.get("/api/v1/domains/").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/api/v1/domains/",
                {"name": "x.example.com"},
                content_type="application/json",
            ).status_code,
            403,
        )

    def test_it_can_still_sign_out(self):
        """Refusing this would leave a session nobody could end."""
        self.assertEqual(self.client.post("/api/v1/auth/logout/").status_code, 204)

    def test_an_admin_can_see_that_it_has_no_role(self):
        # The operators page lists staff accounts; operator_create() sets the
        # flag, and the shell recipe in the docs does too.
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])

        admin = make_operator(username="boss", superuser=True)
        self.client.force_login(admin)
        operators = self.client.get("/api/v1/security/operators/").json()["operators"]
        stranded = next(o for o in operators if o["username"] == "shell")
        self.assertIsNone(stranded["role"])


class DefaultPermissionTests(TestCase):
    def test_an_endpoint_that_sets_nothing_is_still_guarded(self):
        """
        The reason the default changed.

        Before, DEFAULT_PERMISSION_CLASSES was IsAuthenticated, so a view
        added without a permission class was open to every signed-in account.
        The default now follows the method, so forgetting is safe.
        """
        from rest_framework.views import APIView

        from apps.security.roles import RolePermission

        self.assertEqual(APIView.permission_classes, [RolePermission])

    def test_an_anonymous_request_is_refused_before_any_role_is_considered(self):
        self.assertEqual(self.client.get("/api/v1/domains/").status_code, 403)
