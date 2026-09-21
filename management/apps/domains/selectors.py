"""
Domain selectors — read operations.

Following the HackSoft Selectors pattern: functions that return QuerySets
or aggregated data. No side effects.
"""
from django.db.models import QuerySet, Count, Q

from apps.domains.models import Domain


def domain_list(*, is_active: bool | None = None) -> QuerySet[Domain]:
    """List domains, optionally filtered by active status."""
    qs = Domain.objects.all()
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    return qs


def domain_get(*, domain_id: int) -> Domain:
    """Get a single domain by ID. Raises Domain.DoesNotExist."""
    return Domain.objects.select_related("certificate").get(pk=domain_id)


def domain_get_by_name(*, name: str) -> Domain:
    """Get a single domain by hostname. Raises Domain.DoesNotExist."""
    return Domain.objects.get(name=name.strip().lower())


def domain_list_with_rule_counts() -> QuerySet[Domain]:
    """List all domains with their rule counts and certificate state."""
    return (
        Domain.objects
        .select_related("certificate")
        .annotate(
            rule_count=Count("rules", distinct=True),
            active_rule_count=Count(
                "rules", filter=Q(rules__is_active=True), distinct=True
            ),
        )
    )


def domain_stats() -> dict:
    """Aggregate domain statistics for the dashboard."""
    qs = Domain.objects.all()
    return {
        "total": qs.count(),
        "active": qs.filter(is_active=True).count(),
        "ssl_enabled": qs.filter(ssl_enabled=True).count(),
    }
