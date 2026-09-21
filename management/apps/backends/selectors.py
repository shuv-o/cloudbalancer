"""
Backend selectors — read operations.
"""
from django.db.models import QuerySet, Count, Avg, Q, F

from apps.backends.models import Backend, BackendInstance


def backend_list(*, is_active: bool | None = None) -> QuerySet[Backend]:
    """List backends, optionally filtered by active status."""
    qs = Backend.objects.all()
    if is_active is not None:
        qs = qs.filter(is_active=is_active)
    return qs


def backend_get(*, backend_id: int) -> Backend:
    """Get a single backend by ID."""
    return Backend.objects.get(pk=backend_id)


def backend_list_with_stats() -> QuerySet[Backend]:
    """List backends annotated with instance counts and health stats."""
    return Backend.objects.annotate(
        instance_count=Count("instances"),
        healthy_count=Count("instances", filter=Q(instances__is_healthy=True, instances__is_active=True)),
        avg_response_ms=Avg("instances__last_health_response_ms", filter=Q(instances__is_active=True)),
    )


def instance_list(*, backend_id: int) -> QuerySet[BackendInstance]:
    """List all instances of a backend."""
    return BackendInstance.objects.filter(backend_id=backend_id)


def instance_get(*, instance_id: int) -> BackendInstance:
    """Get a single instance by ID."""
    return BackendInstance.objects.get(pk=instance_id)


def backend_health_summary() -> dict:
    """Aggregate health summary for all backends."""
    backends = Backend.objects.filter(is_active=True).prefetch_related("instances")
    summary = []
    for backend in backends:
        instances = backend.instances.filter(is_active=True)
        healthy = instances.filter(is_healthy=True).count()
        total = instances.count()
        summary.append({
            "id": backend.id,
            "name": backend.name,
            "healthy": healthy,
            "total": total,
            "status": "healthy" if healthy == total and total > 0 else (
                "degraded" if healthy > 0 else "down"
            ),
        })
    return {
        "backends": summary,
        "total_backends": len(summary),
        "healthy_backends": sum(1 for b in summary if b["status"] == "healthy"),
    }
