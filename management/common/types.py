"""Common type definitions."""
from dataclasses import dataclass


@dataclass(frozen=True)
class NginxTestResult:
    """Result of running `nginx -t`."""
    success: bool
    output: str


@dataclass(frozen=True)
class HealthCheckResult:
    """Result of a single health check probe."""
    address: str
    port: int
    is_healthy: bool
    status_code: int | None
    response_ms: float | None
    error: str | None = None
    drain_changed: bool = False
    is_draining: bool = False


@dataclass(frozen=True)
class CachePurgeResult:
    """Result of purging one or more cache entries."""
    purged: int
    keys: list[str]
    errors: list[str]
