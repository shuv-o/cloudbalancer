"""
Request-level security controls.

Each of these is a second line behind something Nginx already does, on the
principle that a control which exists in only one place fails completely when
that place is misconfigured.
"""
import logging
import re

from django.http import JsonResponse
from django.utils import timezone

from apps.security.models import PanelAccessPolicy
from apps.security.services import audit_record, client_ip, ip_is_allowed

logger = logging.getLogger(__name__)

# Paths an operator must still reach while they are being forced to enrol.
_TOTP_SETUP_EXEMPT = (
    "/api/v1/auth/",
    "/api/v1/security/totp/",
    "/metrics",
)

# Requests worth recording. Reads are not: they are the overwhelming majority
# of traffic and recording them would bury the changes in noise.
_AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_AUDIT_SUMMARIES = [
    (re.compile(r"^/api/v1/domains/$"), "POST", "Added a domain"),
    (re.compile(r"^/api/v1/domains/\d+/$"), "PATCH", "Changed a domain"),
    (re.compile(r"^/api/v1/domains/\d+/$"), "DELETE", "Deleted a domain"),
    (re.compile(r"^/api/v1/backends/"), "POST", "Added a backend or instance"),
    (re.compile(r"^/api/v1/backends/"), "DELETE", "Removed a backend or instance"),
    (re.compile(r"^/api/v1/routing/rules/"), "POST", "Added a route"),
    (re.compile(r"^/api/v1/routing/rules/"), "PATCH", "Changed a route"),
    (re.compile(r"^/api/v1/routing/rules/"), "DELETE", "Deleted a route"),
    (re.compile(r"^/api/v1/certificates/request/"), "POST", "Requested a certificate"),
    (re.compile(r"^/api/v1/certificates/import/"), "POST", "Uploaded a certificate"),
    (re.compile(r"^/api/v1/certificates/\d+/$"), "DELETE", "Removed a certificate"),
    (re.compile(r"^/api/v1/gateway/deploy/"), "POST", "Deployed the configuration"),
    (re.compile(r"^/api/v1/gateway/cache/"), "POST", "Purged the cache"),
    (re.compile(r"^/api/v1/security/policy/"), "PATCH", "Changed the access policy"),
    (re.compile(r"^/api/v1/security/totp/"), "POST", "Changed a second factor"),
    (re.compile(r"^/api/v1/auth/login/"), "POST", "Signed in"),
    (re.compile(r"^/api/v1/auth/logout/"), "POST", "Signed out"),
]


def _summarise(path: str, method: str) -> str:
    for pattern, wanted_method, summary in _AUDIT_SUMMARIES:
        if pattern.match(path) and method == wanted_method:
            return summary
    return ""


class IpAllowlistMiddleware:
    """
    Refuse addresses outside the allowlist.

    Nginx rejects these first and more cheaply. This exists because a rule that
    lives only in a generated config file disappears the moment that file is
    rendered wrong, and the panel is the one surface where that is not an
    acceptable failure.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        policy = PanelAccessPolicy.load()

        if policy.normalised_allowlist:
            address = client_ip(request)
            if not ip_is_allowed(policy=policy, address=address):
                logger.warning("Refused %s from %s", request.path, address)
                return JsonResponse(
                    {"error": "This address is not allowed to reach the control panel."},
                    status=403,
                )

        return self.get_response(request)


class SessionSecurityMiddleware:
    """
    Expire sessions on idleness and on age.

    Django's own cookie age only covers the second. A session left open on an
    unattended machine is the commonest way an admin panel is misused, and it
    is not something a password or a second factor helps with at all.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)

        if user is not None and user.is_authenticated:
            policy = PanelAccessPolicy.load()
            now = timezone.now().timestamp()

            started = request.session.get("started_at")
            last_seen = request.session.get("last_seen_at")

            if started is None:
                request.session["started_at"] = now
                started = now

            idle_limit = policy.session_idle_minutes * 60
            age_limit = policy.session_max_hours * 3600

            expired_reason = None
            if last_seen is not None and now - last_seen > idle_limit:
                expired_reason = "idle"
            elif now - started > age_limit:
                expired_reason = "age"

            if expired_reason:
                from django.contrib.auth import logout

                logout(request)
                logger.info("Session ended for %s (%s)", user.get_username(), expired_reason)
                return JsonResponse(
                    {
                        "error": (
                            "Your session timed out after a period of inactivity."
                            if expired_reason == "idle"
                            else "Your session reached its maximum length."
                        ),
                        "reason": expired_reason,
                    },
                    status=401,
                )

            request.session["last_seen_at"] = now

        return self.get_response(request)


class RequireTotpMiddleware:
    """
    Hold an operator at enrollment until a second factor exists.

    Turning the requirement on has no effect if existing accounts can carry on
    without one, so the requirement is enforced on every request rather than
    only at sign-in.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)

        if (
            user is not None
            and user.is_authenticated
            and request.path.startswith("/api/")
            and not any(request.path.startswith(prefix) for prefix in _TOTP_SETUP_EXEMPT)
        ):
            from apps.security.services import user_requires_totp_setup

            if user_requires_totp_setup(user=user):
                return JsonResponse(
                    {
                        "error": "Set up an authenticator app before using the panel.",
                        "totp_setup_required": True,
                    },
                    status=403,
                )

        return self.get_response(request)


class AuditMiddleware:
    """Record every request that changed something."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.method in _AUDITED_METHODS and request.path.startswith("/api/"):
            try:
                audit_record(
                    actor=getattr(request, "user", None),
                    action=request.method,
                    path=request.path,
                    status_code=response.status_code,
                    ip_address=client_ip(request),
                    user_agent=request.META.get("HTTP_USER_AGENT", ""),
                    summary=_summarise(request.path, request.method),
                )
            except Exception:
                # An audit failure must never break the request it was recording.
                logger.exception("Could not write an audit record for %s", request.path)

        return response
