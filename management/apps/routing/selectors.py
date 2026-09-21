"""
Routing selectors — read operations.
"""
from django.db.models import Prefetch, QuerySet

from apps.routing.models import ConfigDeployLog, HeaderRoute, RoutingRule


def rule_list(
    *,
    domain_id: int | None = None,
    backend_id: int | None = None,
    is_active: bool | None = None,
) -> QuerySet[RoutingRule]:
    """List routing rules with optional filters."""
    qs = (
        RoutingRule.objects
        .select_related("domain", "backend")
        .prefetch_related(
            Prefetch(
                "header_routes",
                queryset=HeaderRoute.objects.select_related("backend"),
            )
        )
    )
    if domain_id is not None:
        qs = qs.filter(domain_id=domain_id)
    if backend_id is not None:
        qs = qs.filter(backend_id=backend_id)
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    return qs


def rule_get(*, rule_id: int) -> RoutingRule:
    """Get a single routing rule by ID."""
    return (
        RoutingRule.objects
        .select_related("domain", "backend")
        .prefetch_related("header_routes__backend")
        .get(pk=rule_id)
    )


def header_route_get(*, header_route_id: int) -> HeaderRoute:
    return HeaderRoute.objects.select_related("rule", "backend").get(pk=header_route_id)


def header_route_list(*, rule_id: int) -> QuerySet[HeaderRoute]:
    return HeaderRoute.objects.select_related("backend").filter(rule_id=rule_id)


def deploy_log_list(*, limit: int = 20) -> QuerySet[ConfigDeployLog]:
    """List recent config deploy logs."""
    return ConfigDeployLog.objects.select_related("deployed_by")[:limit]


def deploy_log_get(*, log_id: int) -> ConfigDeployLog:
    """Get a single deploy log entry."""
    return ConfigDeployLog.objects.select_related("deployed_by").get(pk=log_id)


def routing_stats() -> dict:
    """Rule counts for the dashboard."""
    qs = RoutingRule.objects.all()
    active = qs.filter(is_active=True)
    return {
        "total_rules": qs.count(),
        "active_rules": active.count(),
        "cached_rules": active.filter(cache_enabled=True).count(),
        "rate_limited_rules": active.filter(rate_limit_enabled=True).count(),
        "header_routes": HeaderRoute.objects.filter(is_active=True).count(),
    }
