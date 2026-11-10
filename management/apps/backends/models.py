"""
Backend models.

A Backend represents a logical service (e.g. "user-service") with one or more
instances running on the LAN. BackendInstance represents a single running copy
of that service at a specific host:port.
"""
from django.core.exceptions import ValidationError
from django.db import models

from common.validators import validate_identifier, validate_range


# A port whose meaning is fixed by convention, paired with the wrong scheme, is
# almost always a mistake -- and the symptom does not point at the cause.
# Cleartext sent to a TLS port makes a router answer with its catch-all 404,
# which reads as "no route for this host" rather than "wrong protocol", and a
# gateway in front of it will happily cache that 404.
_SCHEME_PORT_CONFLICTS = {
    ("http", 443): (
        "Port 443 is a TLS port, but this backend's protocol is HTTP. Nginx "
        "would open a cleartext socket to it and the backend would reject or "
        "misroute the request. Set the protocol to HTTPS, or change the port."
    ),
    ("https", 80): (
        "Port 80 is a cleartext port, but this backend's protocol is HTTPS. "
        "Nginx would attempt a TLS handshake against it and fail. Set the "
        "protocol to HTTP, or change the port."
    ),
}


def scheme_port_conflict(scheme: str, port: int) -> str | None:
    """Return an explanation if this scheme and port cannot work together."""
    return _SCHEME_PORT_CONFLICTS.get((scheme, port))


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

    class Scheme(models.TextChoices):
        HTTP = "http", "HTTP"
        HTTPS = "https", "HTTPS"

    scheme = models.CharField(
        max_length=5,
        choices=Scheme.choices,
        default=Scheme.HTTP,
        help_text=(
            "How to reach this backend. Plain HTTP is right for a service on a "
            "trusted LAN -- TLS terminates once, at the gateway, and the hop "
            "costs nothing. Choose HTTPS when the backend insists on it, or "
            "when the network between here and it is not yours."
        ),
    )
    tls_verify = models.BooleanField(
        default=False,
        help_text=(
            "Check the backend's certificate against a trusted CA. Off by "
            "default because an internal backend usually presents a self-signed "
            "or private-CA certificate, and failing closed on that would take "
            "the route down rather than secure it."
        ),
    )
    tls_trusted_ca_path = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="PEM bundle to verify the backend against, when verification is on",
    )
    tls_server_name = models.CharField(
        max_length=253,
        blank=True,
        default="",
        help_text=(
            "SNI to send to the backend. Blank forwards the hostname the client "
            "asked for, which is what a backend router like Traefik needs in "
            "order to match its own rules."
        ),
    )

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

    def clean(self):
        """Reject a scheme that contradicts a port already configured."""
        super().clean()
        # The name becomes an upstream block name, which cannot be quoted.
        validate_identifier(self.name, label="backend name")
        validate_range(
            self.keepalive_connections, low=0, high=10000,
            label="keepalive connections",
        )
        validate_range(
            self.health_check_interval, low=1, high=86400,
            label="health check interval", unit="s",
        )
        validate_range(
            self.health_check_timeout, low=1, high=300,
            label="health check timeout", unit="s",
        )
        if self.health_check_timeout >= self.health_check_interval:
            raise ValidationError({
                "health_check_timeout": (
                    f"A {self.health_check_timeout}s timeout does not fit in a "
                    f"{self.health_check_interval}s interval -- probes would "
                    "overlap and queue. Shorten the timeout or lengthen the "
                    "interval."
                )
            })
        validate_range(
            self.unhealthy_threshold, low=1, high=100,
            label="unhealthy threshold",
        )
        validate_range(
            self.healthy_threshold, low=1, high=100, label="healthy threshold"
        )
        if self.pk is None:
            return
        for instance in self.instances.all():
            message = scheme_port_conflict(self.scheme, instance.port)
            if message:
                raise ValidationError(
                    {"scheme": f"{instance.address}:{instance.port} -- {message}"}
                )

    @property
    def upstream_name(self) -> str:
        """Nginx-safe upstream block name."""
        return self.name.replace("-", "_").replace(".", "_")

    # These three are for a single backend, where one query each is fine.
    # List endpoints must not use them: see backend_list_with_stats, which
    # annotates the same names so the serializer never reaches the property.
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

    def clean(self):
        """Reject a port that contradicts the backend's scheme."""
        super().clean()
        validate_range(self.port, low=1, high=65535, label="port")
        validate_range(self.weight, low=1, high=1000, label="weight")
        validate_range(self.max_fails, low=0, high=1000, label="max fails")
        validate_range(
            self.fail_timeout, low=1, high=86400, label="fail timeout", unit="s"
        )
        if self.backend_id is None:
            return
        message = scheme_port_conflict(self.backend.scheme, self.port)
        if message:
            raise ValidationError({"port": message})

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
