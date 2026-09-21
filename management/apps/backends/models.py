"""
Backend models.

A Backend represents a logical service (e.g. "user-service") with one or more
instances running on the LAN. BackendInstance represents a single running copy
of that service at a specific host:port.
"""
from django.db import models


class Backend(models.Model):
    """
    A logical backend service that receives proxied traffic.

    Maps to an Nginx `upstream` block.
    """

    class LBMethod(models.TextChoices):
        ROUND_ROBIN = "round_robin", "Round Robin"
        LEAST_CONN = "least_conn", "Least Connections"
        IP_HASH = "ip_hash", "IP Hash"

    name = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Logical service name, e.g. user-service",
    )
    description = models.TextField(blank=True, default="")

    # Load balancing
    lb_method = models.CharField(
        max_length=20,
        choices=LBMethod.choices,
        default=LBMethod.ROUND_ROBIN,
        verbose_name="Load balancing method",
    )

    # Keepalive connections to upstream instances
    keepalive_connections = models.IntegerField(
        default=32,
        help_text=(
            "Idle connections each worker keeps open to this backend. "
            "Size it to peak concurrent in-flight requests per worker, not total."
        ),
    )
    keepalive_requests = models.IntegerField(
        default=1000,
        help_text="Requests to send down one connection before reopening it",
    )
    keepalive_timeout = models.IntegerField(
        default=60,
        help_text="Seconds an idle connection to this backend is kept open",
    )

    # Health checking
    health_check_enabled = models.BooleanField(default=True)
    health_check_path = models.CharField(
        max_length=255,
        default="/health",
        help_text="HTTP path to probe for health checks",
    )
    health_check_interval = models.IntegerField(
        default=10,
        help_text="Seconds between health checks",
    )
    health_check_timeout = models.IntegerField(
        default=5,
        help_text="Seconds before a health check is considered failed",
    )
    drain_unhealthy = models.BooleanField(
        default=False,
        help_text=(
            "Write failing instances out of the gateway config as 'down'. "
            "Off by default: Nginx already fails over on its own, in real time, "
            "and every drain costs a reload."
        ),
    )
    unhealthy_threshold = models.IntegerField(
        default=3,
        help_text="Consecutive failed probes before an instance is drained",
    )
    healthy_threshold = models.IntegerField(
        default=2,
        help_text="Consecutive successful probes before a drained instance returns",
    )

    # State
    is_active = models.BooleanField(default=True, db_index=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def upstream_name(self) -> str:
        """Nginx-safe upstream block name."""
        return self.name.replace("-", "_").replace(".", "_")

    @property
    def healthy_instance_count(self) -> int:
        return self.instances.filter(is_healthy=True, is_active=True).count()

    @property
    def serving_instance_count(self) -> int:
        """Instances Nginx will actually send traffic to."""
        return self.instances.filter(is_active=True, is_draining=False).count()

    @property
    def total_instance_count(self) -> int:
        return self.instances.count()


class BackendInstance(models.Model):
    """
    A single running instance of a backend service.

    Maps to a `server` directive inside an Nginx `upstream` block.
    """

    backend = models.ForeignKey(
        Backend,
        on_delete=models.CASCADE,
        related_name="instances",
    )
    address = models.GenericIPAddressField(
        help_text="IP address of the backend instance",
    )
    port = models.IntegerField(
        default=8080,
        help_text="Port the backend instance listens on",
    )
    weight = models.IntegerField(
        default=1,
        help_text="Relative weight for load balancing (higher = more traffic)",
    )
    max_fails = models.IntegerField(
        default=3,
        help_text="Number of failed requests before marking as unavailable",
    )
    fail_timeout = models.IntegerField(
        default=10,
        help_text="Seconds to wait before retrying a failed upstream",
    )

    # Health status (updated by health checker)
    is_healthy = models.BooleanField(default=True, db_index=True)
    consecutive_failures = models.IntegerField(default=0)
    consecutive_successes = models.IntegerField(default=0)
    is_draining = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Rendered as 'down' in the upstream block, so it takes no new traffic",
    )
    last_health_check = models.DateTimeField(null=True, blank=True)
    last_health_status_code = models.IntegerField(null=True, blank=True)
    last_health_response_ms = models.FloatField(
        null=True,
        blank=True,
        help_text="Response time of the last health check in milliseconds",
    )

    # State
    is_active = models.BooleanField(default=True, db_index=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["backend", "address", "port"]
        unique_together = [("backend", "address", "port")]

    def __str__(self) -> str:
        status = "✓" if self.is_healthy else "✗"
        return f"{self.backend.name} → {self.address}:{self.port} [{status}]"

    @property
    def netloc(self) -> str:
        """host:port string for Nginx server directive."""
        return f"{self.address}:{self.port}"

    @property
    def state(self) -> str:
        """Single word for the dashboard: what this instance is doing."""
        if not self.is_active:
            return "disabled"
        if self.is_draining:
            return "draining"
        return "healthy" if self.is_healthy else "failing"
