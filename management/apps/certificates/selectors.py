"""Certificate selectors — read operations."""
from datetime import timedelta

from django.db.models import Q, QuerySet
from django.utils import timezone

from apps.certificates.models import AcmeAccount, Certificate


def certificate_list(*, status: str | None = None) -> QuerySet[Certificate]:
    qs = Certificate.objects.select_related("domain", "acme_account")
    if status is not None:
        qs = qs.filter(status=status)
    return qs


def certificate_get(*, certificate_id: int) -> Certificate:
    return Certificate.objects.select_related("domain", "acme_account").get(pk=certificate_id)


def certificate_get_for_domain(*, domain_id: int) -> Certificate:
    return Certificate.objects.select_related("domain", "acme_account").get(domain_id=domain_id)


def certificate_list_due_for_renewal() -> QuerySet[Certificate]:
    """
    Certificates inside their renewal window, plus ones that have never
    succeeded. The day threshold lives on each row, so the filter compares
    against the widest window and the model property does the exact check.
    """
    widest = (
        Certificate.objects.filter(auto_renew=True).order_by("-renew_before_days")
        .values_list("renew_before_days", flat=True).first()
        or 30
    )
    horizon = timezone.now() + timedelta(days=widest)
    return (
        Certificate.objects
        .select_related("domain", "acme_account")
        .filter(auto_renew=True, issuer=Certificate.Issuer.ACME)
        .filter(Q(not_after__lte=horizon) | Q(not_after__isnull=True))
        .exclude(status__in=[Certificate.Status.REQUESTING, Certificate.Status.RENEWING])
    )


def certificate_expiry_summary() -> dict:
    """Counts the SSL page and the dashboard header both need."""
    now = timezone.now()
    qs = Certificate.objects.all()
    return {
        "total": qs.count(),
        "active": qs.filter(status=Certificate.Status.ACTIVE).count(),
        "failed": qs.filter(status=Certificate.Status.FAILED).count(),
        "pending": qs.filter(status=Certificate.Status.PENDING).count(),
        "expiring_within_14d": qs.filter(
            not_after__gt=now, not_after__lte=now + timedelta(days=14)
        ).count(),
        "expired": qs.filter(not_after__lte=now).count(),
        "auto_renewing": qs.filter(
            auto_renew=True, issuer=Certificate.Issuer.ACME
        ).count(),
        "next_expiry": (
            qs.filter(not_after__gt=now)
            .order_by("not_after")
            .values("domain__name", "not_after")
            .first()
        ),
    }


def acme_account_list() -> QuerySet[AcmeAccount]:
    return AcmeAccount.objects.all()


def acme_account_get(*, account_id: int) -> AcmeAccount:
    return AcmeAccount.objects.get(pk=account_id)
