"""
Celery tasks for the gateway control plane.

Every deploy is a graceful Nginx reload, and every reload starts fresh workers
whose upstream keepalive pools are empty. Editing five routing rules in the
control panel should therefore not produce five reloads. `request_config_deploy`
coalesces a burst of edits into one deploy a few seconds later, which is the
difference between a config change being free and being a latency event.
"""
import logging

from celery import shared_task
from django.conf import settings
from django.core.cache import cache

from apps.backends.models import Backend
from apps.backends.services import health_check_backend
from apps.gateway.services import (
    cache_purge_all,
    cache_purge_url,
    config_deploy,
)

logger = logging.getLogger(__name__)

_SCHEDULED_KEY = "gateway:deploy:scheduled"
_LOCK_KEY = "gateway:deploy:lock"
_PENDING_USER_KEY = "gateway:deploy:requested_by"


def request_config_deploy(*, user_id: int | None = None, delay: int | None = None) -> bool:
    """
    Ask for a deploy, coalescing anything already queued.

    Returns True when this call scheduled the deploy, False when it joined one
    already pending. Safe to call from every write endpoint.
    """
    delay = settings.GATEWAY_DEPLOY_DEBOUNCE_SECONDS if delay is None else delay

    if user_id is not None:
        cache.set(_PENDING_USER_KEY, user_id, timeout=delay + 60)

    # add() is atomic in Redis: only the first caller in the window wins.
    if not cache.add(_SCHEDULED_KEY, True, timeout=delay + 30):
        logger.debug("Deploy already scheduled; joining it")
        return False

    async_config_deploy.apply_async(countdown=delay)
    logger.info("Deploy scheduled in %ss", delay)
    return True


@shared_task(bind=True, name="gateway.deploy", max_retries=1, default_retry_delay=10)
def async_config_deploy(self, user_id: int | None = None):
    """Render the config from the database, verify it, and reload Nginx."""
    from django.contrib.auth.models import User

    cache.delete(_SCHEDULED_KEY)

    if user_id is None:
        user_id = cache.get(_PENDING_USER_KEY)
    cache.delete(_PENDING_USER_KEY)

    # One deploy at a time. Two concurrent renders would race on the same
    # files and could leave a half-written config behind.
    if not cache.add(_LOCK_KEY, True, timeout=120):
        logger.info("Another deploy holds the lock; re-queueing")
        raise self.retry(countdown=5)

    try:
        user = User.objects.filter(pk=user_id).first() if user_id else None
        log = config_deploy(user=user)
        return {
            "log_id": log.pk,
            "status": log.status,
            "error": log.error_output or None,
        }
    finally:
        cache.delete(_LOCK_KEY)


@shared_task(name="gateway.health_check_all")
def async_health_check_all():
    """
    Probe every active backend.

    If any instance crossed its drain threshold, one deploy is requested at the
    end — not one per instance.
    """
    results, drain_changed = {}, False

    for backend in Backend.objects.filter(is_active=True, health_check_enabled=True):
        try:
            checks = health_check_backend(backend=backend)
        except Exception as exc:
            logger.exception("Health check failed for %s", backend.name)
            results[backend.name] = {"error": str(exc)}
            continue

        drain_changed = drain_changed or any(c.drain_changed for c in checks)
        results[backend.name] = {
            "total": len(checks),
            "healthy": sum(1 for c in checks if c.is_healthy),
            "draining": sum(1 for c in checks if c.is_draining),
        }

    if drain_changed:
        logger.info("Instance drain state changed; requesting a deploy")
        request_config_deploy()

    return results


@shared_task(name="gateway.health_check_backend")
def async_health_check_backend(backend_id: int):
    """Probe one backend, on demand from the control panel."""
    backend = Backend.objects.filter(pk=backend_id).first()
    if backend is None:
        return {"error": f"Backend {backend_id} no longer exists"}

    checks = health_check_backend(backend=backend)
    if any(c.drain_changed for c in checks):
        request_config_deploy()

    return {
        "backend": backend.name,
        "total": len(checks),
        "healthy": sum(1 for c in checks if c.is_healthy),
        "draining": sum(1 for c in checks if c.is_draining),
    }


@shared_task(name="gateway.cache_purge_all")
def async_cache_purge_all():
    result = cache_purge_all()
    return {"purged": result.purged, "errors": result.errors}


@shared_task(name="gateway.cache_purge_url")
def async_cache_purge_url(host: str, uri: str, scheme: str = "https", method: str = "GET"):
    result = cache_purge_url(host=host, uri=uri, scheme=scheme, method=method)
    return {"purged": result.purged, "keys": result.keys, "errors": result.errors}
