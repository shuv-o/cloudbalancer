"""
Backend services — write operations.
"""
import socket
import time

import httpx
from django.db import transaction
from django.utils import timezone as django_tz

from apps.backends.health import matches as status_matches
from apps.backends.models import Backend, BackendInstance
from common.services import model_update
from common.types import HealthCheckResult


def backend_create(
    *,
    name: str,
    description: str = "",
    scheme: str = Backend.Scheme.HTTP,
    tls_verify: bool = False,
    tls_trusted_ca_path: str = "",
    tls_server_name: str = "",
    lb_method: str = Backend.LBMethod.ROUND_ROBIN,
    keepalive_connections: int = 32,
    keepalive_requests: int = 1000,
    keepalive_timeout: int = 60,
    health_check_enabled: bool = True,
    health_check_method: str = "http",
    health_check_expect: str = "200-399",
    health_check_path: str = "/health",
    health_check_host: str = "",
    health_check_interval: int = 10,
    health_check_timeout: int = 5,
    drain_unhealthy: bool = False,
    unhealthy_threshold: int = 3,
    healthy_threshold: int = 2,
    is_active: bool = True,
) -> Backend:
    """Create a new backend service."""
    backend = Backend(
        name=name.strip(),
        description=description,
        scheme=scheme,
        tls_verify=tls_verify,
        tls_trusted_ca_path=tls_trusted_ca_path,
        tls_server_name=tls_server_name,
        lb_method=lb_method,
        keepalive_connections=keepalive_connections,
        keepalive_requests=keepalive_requests,
        keepalive_timeout=keepalive_timeout,
        health_check_enabled=health_check_enabled,
        health_check_method=health_check_method,
        health_check_expect=health_check_expect,
        health_check_path=health_check_path,
        health_check_host=health_check_host,
        health_check_interval=health_check_interval,
        health_check_timeout=health_check_timeout,
        drain_unhealthy=drain_unhealthy,
        unhealthy_threshold=unhealthy_threshold,
        healthy_threshold=healthy_threshold,
        is_active=is_active,
    )
    backend.full_clean()
    backend.save()
    return backend


def backend_update(*, backend: Backend, data: dict) -> Backend:
    """Update a backend's fields."""
    updatable_fields = [
        "name",
        "description",
        "scheme",
        "tls_verify",
        "tls_trusted_ca_path",
        "tls_server_name",
        "lb_method",
        "keepalive_connections",
        "health_check_enabled",
        "health_check_method",
        "health_check_expect",
        "health_check_path",
        "health_check_host",
        "health_check_interval",
        "health_check_timeout",
        "drain_unhealthy",
        "unhealthy_threshold",
        "healthy_threshold",
        "is_active",
    ]
    instance, _ = model_update(instance=backend, fields=updatable_fields, data=data)
    return instance


@transaction.atomic
def backend_delete(*, backend: Backend) -> None:
    """Delete a backend and all its instances (cascaded)."""
    backend.delete()


# ---------------------------------------------------------------------------
# Instances
# ---------------------------------------------------------------------------

def instance_add(
    *,
    backend: Backend,
    address: str,
    port: int = 8080,
    weight: int = 1,
    max_fails: int = 3,
    fail_timeout: int = 10,
    is_active: bool = True,
) -> BackendInstance:
    """Add a new instance to a backend."""
    instance = BackendInstance(
        backend=backend,
        address=address,
        port=port,
        weight=weight,
        max_fails=max_fails,
        fail_timeout=fail_timeout,
        is_active=is_active,
    )
    instance.full_clean()
    instance.save()
    return instance


def instance_update(*, instance: BackendInstance, data: dict) -> BackendInstance:
    """Update an instance's fields."""
    updatable_fields = ["address", "port", "weight", "max_fails", "fail_timeout", "is_active"]
    obj, _ = model_update(instance=instance, fields=updatable_fields, data=data)
    return obj


def instance_delete(*, instance: BackendInstance) -> None:
    """Remove an instance from its backend."""
    instance.delete()


# ---------------------------------------------------------------------------
# Health Checks
# ---------------------------------------------------------------------------

def health_check_instance(
    *,
    instance: BackendInstance,
    method: str = "http",
    expect: str = "200-399",
    scheme: str = "http",
    host: str = "",
    path: str = "/health",
    timeout: int = 5,
    verify: bool = False,
    unhealthy_threshold: int = 3,
    healthy_threshold: int = 2,
    drain_unhealthy: bool = False,
) -> HealthCheckResult:
    """
    Probe one instance and record the result.

    Nginx does its own passive failover in real time, so this probe exists to
    tell the operator what is happening, not to steer traffic. It only steers
    traffic when the backend opts in with `drain_unhealthy`, and even then it
    waits for several consecutive results in a row: a probe that flipped an
    instance on every blip would turn one flapping backend into a continuous
    stream of gateway reloads.
    """
    start = time.monotonic()

    try:
        if method == Backend.HealthCheckMethod.TCP:
            # No request, no status. Something is listening or it is not --
            # which is the only question that can be asked of a backend with
            # no health endpoint, or one that does not speak HTTP.
            socket.create_connection(
                (instance.address, instance.port), timeout=timeout
            ).close()
            elapsed_ms = (time.monotonic() - start) * 1000
            result = HealthCheckResult(
                address=instance.address,
                port=instance.port,
                is_healthy=True,
                status_code=None,
                response_ms=round(elapsed_ms, 2),
            )
        else:
            # The scheme has to follow the backend's own. Probing an https
            # backend over plaintext is the same mistake that once sent live
            # traffic to a TLS port, and here it produces a permanently
            # failing probe against a healthy host.
            url = f"{scheme}://{instance.address}:{instance.port}{path}"

            # Addressed by IP, a reverse proxy that routes on hostname matches
            # no route and answers 404 however healthy the service behind it
            # is. The Host header makes the probe ask the question the
            # operator meant.
            headers = {"Host": host} if host else None

            # verify is off by default because the probe connects to an IP, so
            # the certificate legitimately will not match the name on it.
            with httpx.Client(timeout=timeout, verify=verify) as client:
                response = client.get(url, headers=headers)
            elapsed_ms = (time.monotonic() - start) * 1000

            result = HealthCheckResult(
                address=instance.address,
                port=instance.port,
                is_healthy=status_matches(expect, response.status_code),
                status_code=response.status_code,
                response_ms=round(elapsed_ms, 2),
            )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        result = HealthCheckResult(
            address=instance.address,
            port=instance.port,
            is_healthy=False,
            status_code=None,
            response_ms=round(elapsed_ms, 2),
            error=str(exc),
        )

    if result.is_healthy:
        instance.consecutive_successes += 1
        instance.consecutive_failures = 0
    else:
        instance.consecutive_failures += 1
        instance.consecutive_successes = 0

    drain_changed = False
    if drain_unhealthy:
        if not instance.is_draining and instance.consecutive_failures >= unhealthy_threshold:
            instance.is_draining = True
            drain_changed = True
        elif instance.is_draining and instance.consecutive_successes >= healthy_threshold:
            instance.is_draining = False
            drain_changed = True
    elif instance.is_draining:
        # The operator turned draining off; put the instance back in rotation.
        instance.is_draining = False
        drain_changed = True

    instance.is_healthy = result.is_healthy
    instance.last_health_check = django_tz.now()
    instance.last_health_status_code = result.status_code
    instance.last_health_response_ms = result.response_ms
    instance.save(
        update_fields=[
            "is_healthy",
            "consecutive_failures",
            "consecutive_successes",
            "is_draining",
            "last_health_check",
            "last_health_status_code",
            "last_health_response_ms",
            "updated_at",
        ]
    )

    return HealthCheckResult(
        address=result.address,
        port=result.port,
        is_healthy=result.is_healthy,
        status_code=result.status_code,
        response_ms=result.response_ms,
        error=result.error,
        drain_changed=drain_changed,
        is_draining=instance.is_draining,
    )


def health_check_backend(*, backend: Backend) -> list[HealthCheckResult]:
    """Run health checks against all active instances of a backend."""
    return [
        health_check_instance(
            instance=inst,
            method=backend.health_check_method,
            expect=backend.health_check_expect,
            scheme=backend.scheme,
            host=backend.health_check_host,
            path=backend.health_check_path,
            verify=backend.tls_verify,
            timeout=backend.health_check_timeout,
            unhealthy_threshold=backend.unhealthy_threshold,
            healthy_threshold=backend.healthy_threshold,
            drain_unhealthy=backend.drain_unhealthy,
        )
        for inst in backend.instances.filter(is_active=True)
    ]
