"""Security selectors — read operations."""
from datetime import timedelta

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from apps.security.models import (
    AccountLock,
    AuditEvent,
    LoginAttempt,
    PanelAccessPolicy,
    TotpDevice,
)


def policy_get() -> PanelAccessPolicy:
    return PanelAccessPolicy.load()


def audit_list(*, limit: int = 100, actor_id: int | None = None) -> QuerySet[AuditEvent]:
    qs = AuditEvent.objects.select_related("actor")
    if actor_id is not None:
        qs = qs.filter(actor_id=actor_id)
    return qs[:limit]


def login_attempt_list(*, limit: int = 100, failures_only: bool = False) -> QuerySet[LoginAttempt]:
    qs = LoginAttempt.objects.all()
    if failures_only:
        qs = qs.exclude(outcome=LoginAttempt.Outcome.SUCCESS)
    return qs[:limit]


def active_locks() -> QuerySet[AccountLock]:
    return AccountLock.objects.filter(locked_until__gt=timezone.now())


def security_summary() -> dict:
    """
    The state of the panel's own defences.

    Written to answer one question at a glance: is anything currently weaker
    than it should be, and is anyone trying?
    """
    from django.contrib.auth.models import User

    policy = PanelAccessPolicy.load()
    last_24h = timezone.now() - timedelta(hours=24)

    recent = LoginAttempt.objects.filter(created_at__gte=last_24h)
    outcomes = dict(
        recent.values_list("outcome").annotate(n=Count("id")).values_list("outcome", "n")
    )

    operators = User.objects.filter(is_active=True, is_staff=True)
    with_totp = TotpDevice.objects.filter(
        confirmed_at__isnull=False, user__in=operators
    ).count()

    failures = sum(v for k, v in outcomes.items() if k != LoginAttempt.Outcome.SUCCESS)

    return {
        "published": policy.is_published,
        "panel_domain": policy.panel_domain,
        "warnings": policy.exposure_warnings,
        "controls": {
            "ip_allowlist": policy.normalised_allowlist,
            "require_mtls": policy.require_mtls,
            "require_totp": policy.require_totp,
            "lockout_threshold": policy.lockout_threshold,
            "expose_django_admin": policy.expose_django_admin,
        },
        "operators": {
            "total": operators.count(),
            "with_second_factor": with_totp,
            "without_second_factor": max(operators.count() - with_totp, 0),
        },
        "last_24h": {
            "successful_sign_ins": outcomes.get(LoginAttempt.Outcome.SUCCESS, 0),
            "failed_attempts": failures,
            "by_outcome": outcomes,
            "distinct_addresses": recent.values("ip_address").distinct().count(),
        },
        "locked_accounts": [
            {"username": lock.username, "minutes_remaining": lock.minutes_remaining}
            for lock in active_locks()
        ],
        "changes_last_24h": AuditEvent.objects.filter(created_at__gte=last_24h).count(),
    }


def operator_list() -> list[dict]:
    """Every operator account and whether it has a second factor."""
    from django.contrib.auth.models import User

    devices = {
        d.user_id: d
        for d in TotpDevice.objects.filter(confirmed_at__isnull=False)
    }
    return [
        {
            "id": user.pk,
            "username": user.get_username(),
            "email": user.email,
            "is_superuser": user.is_superuser,
            "is_active": user.is_active,
            "last_login": user.last_login,
            "has_second_factor": user.pk in devices,
            "recovery_codes_remaining": (
                devices[user.pk].recovery_codes_remaining if user.pk in devices else 0
            ),
        }
        for user in User.objects.filter(is_staff=True).order_by("username")
    ]
