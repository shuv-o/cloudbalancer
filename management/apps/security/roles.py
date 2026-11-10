"""
Who may do what.

Before this, the panel had two levels and the gap between them was enormous:
`IsSuperuser` guarded the security endpoints, and everything else took
`IsAuthenticated`. So an account that could not change the IP allowlist could
still delete every domain, reissue every certificate and redeploy the gateway.
There was no account that could only look.

Three roles, ordered, each a superset of the one before:

    viewer      reads everything the panel shows. Changes nothing.
    operator    configures traffic: domains, backends, routes, certificates,
                deploys, cache. Cannot change who is allowed in.
    admin       everything, including other operators and the access policy.

Two decisions worth stating, because neither is obvious:

*Reads are not uniformly viewer-level.* The audit log and the sign-in attempt
history are admin-only. They are a record of who was where and when, including
failed guesses against named accounts; handing that to everyone who can read a
dashboard inverts the reason it is kept.

*The default is the authenticated user's role, not a fixed class.* An endpoint
added later is covered by whatever its HTTP method implies -- reads need a
viewer, writes need an operator -- rather than being open to anyone signed in
because nobody remembered to restrict it. A view that needs something else says
so with `required_role`, which is visible in the view and reads as intent.
"""
from django.db import models
from rest_framework.permissions import SAFE_METHODS, BasePermission


class Role(models.TextChoices):
    VIEWER = "viewer", "Viewer"
    OPERATOR = "operator", "Operator"
    ADMIN = "admin", "Admin"


# Ascending. Comparison is on this, never on the string, so adding a role
# between two existing ones does not silently reorder the others.
RANK = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}

# Shown in the panel when choosing a role for someone. Phrased as what the
# person will be able to do, not as which endpoints are permitted.
DESCRIPTIONS = {
    Role.VIEWER: (
        "Sees every domain, route, backend and chart. Changes nothing, and "
        "cannot see the audit log or sign-in history."
    ),
    Role.OPERATOR: (
        "Everything a viewer sees, plus adding and changing domains, "
        "backends, routes and certificates, and deploying the gateway. "
        "Cannot change who is allowed to reach the panel."
    ),
    Role.ADMIN: (
        "Everything, including adding operators, changing their roles, and "
        "changing how the panel itself may be reached."
    ),
}


def role_of(user) -> str | None:
    """
    The role this account holds, or None if it holds none.

    A superuser is always an admin. The Django flag is what `createsuperuser`
    sets and what the shell reaches for, so an installation always has a way
    back in even if every profile row were wrong -- and an admin who could be
    demoted out of the panel by a bad migration is a lockout waiting to happen.
    """
    if not (user and user.is_authenticated):
        return None
    if user.is_superuser:
        return Role.ADMIN

    profile = getattr(user, "operator_profile", None)
    return profile.role if profile else None


def has_at_least(user, required: str) -> bool:
    """
    Whether this account holds `required` or better.

    An account with no profile holds nothing and is refused. That is
    deliberate: a user created straight in the shell, or left behind by a
    half-finished migration, gets no access rather than inheriting whatever
    the default happens to be. The operators page shows such an account as
    having no role, so the fix is visible rather than guessed at.
    """
    current = role_of(user)
    if current is None:
        return False
    return RANK[Role(current)] >= RANK[Role(required)]


class RolePermission(BasePermission):
    """
    The default permission for every endpoint in the panel.

    `required_role` on the view overrides the method-derived default. Set it
    to Role.VIEWER on a write that is a person acting on their own account --
    enrolling their own authenticator, removing their own passkey -- because
    those are not configuration changes and a viewer must be able to do them.
    Without that, the second-factor requirement and the role system together
    lock a viewer out of the panel entirely: held at enrolment, and refused
    permission to enrol.
    """

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated):
            return False

        required = getattr(view, "required_role", None)
        if required is None:
            required = Role.VIEWER if request.method in SAFE_METHODS else Role.OPERATOR

        return has_at_least(user, required)

    def message(self, *args, **kwargs):  # pragma: no cover - DRF reads the attr
        return "Your role does not allow that."


class IsAdmin(RolePermission):
    """For a view where admin is the whole rule, rather than a raised floor."""

    def has_permission(self, request, view) -> bool:
        return has_at_least(getattr(request, "user", None), Role.ADMIN)
