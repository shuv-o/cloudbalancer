"""
Routing models.

A RoutingRule maps an incoming request (matched by path prefix, exact path,
or subdomain) to a specific Backend. Rules are scoped to a Domain and have
a priority for ordering.
"""
from django.db import models


class RoutingRule(models.Model):
    """
    A single routing rule: domain + match → backend.

    Maps to an Nginx `location` block inside the domain's `server` block.
    Lower priority number = evaluated first (like firewall rules).
    """

    class MatchType(models.TextChoices):
        PATH_PREFIX = "path_prefix", "Path Prefix"
        EXACT_PATH = "exact_path", "Exact Path"
        REGEX = "regex", "Regular Expression"

    domain = models.ForeignKey(
        "domains.Domain",
        on_delete=models.CASCADE,
        related_name="rules",
    )
    backend = models.ForeignKey(
        "backends.Backend",
        on_delete=models.CASCADE,
        related_name="rules",
    )

    # Match specification
    match_type = models.CharField(
        max_length=20,
        choices=MatchType.choices,
        default=MatchType.PATH_PREFIX,
    )
    match_value = models.CharField(
        max_length=255,
        help_text="Path prefix (e.g. /api/v1) or exact path (e.g. /health)",
    )

    # Priority — lower = higher priority
    priority = models.IntegerField(
        default=100,
        db_index=True,
        help_text="Lower number = higher priority (evaluated first)",
    )

    # Caching
    cache_enabled = models.BooleanField(
        default=False,
        help_text="Enable HTTP caching at the gateway for this route",
    )
    cache_ttl = models.IntegerField(
        default=600,
        help_text="Cache TTL in seconds (for 200/302 responses)",
    )
    cache_bypass_auth = models.BooleanField(
        default=True,
        help_text="Bypass cache when Authorization header or session cookie is present",
    )
    cache_ignore_query_string = models.BooleanField(
        default=False,
        help_text=(
            "Cache by path alone, ignoring the query string. Without this, a "
            "flood of requests carrying random query parameters misses on every "
            "one and hands the whole load straight to the backend -- the cache "
            "stops being a shield and becomes a liability. Only correct where "
            "the query string does not change the response."
        ),
    )
    cache_min_uses = models.IntegerField(
        default=1,
        help_text=(
            "Store a response only after this many requests for it. Raising it "
            "keeps one-off URLs from filling the cache."
        ),
    )
    cache_key_headers = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "Request headers to fold into the cache key, e.g. [\"Accept-Encoding\"]. "
            "Any header this route switches on must be listed here or the cache "
            "will serve one variant to every caller."
        ),
    )
    cache_ignore_upstream_control = models.BooleanField(
        default=False,
        help_text=(
            "Cache even when the backend sends Cache-Control: no-cache. "
            "Off by default because it overrides the backend's own judgement."
        ),
    )
    cache_allow_authenticated = models.BooleanField(
        default=False,
        help_text=(
            "Acknowledgement that caching authenticated responses is intended. "
            "Required before cache_bypass_auth can be turned off."
        ),
    )

    # Request manipulation
    strip_prefix = models.BooleanField(
        default=False,
        help_text="Strip the match_value prefix before forwarding to backend",
    )
    custom_headers = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra headers to send to the backend (JSON object)",
    )

    # Proxy tuning
    proxy_buffering = models.BooleanField(
        default=True,
        help_text="Enable response buffering (disable for WebSocket/SSE)",
    )
    proxy_read_timeout = models.IntegerField(
        default=60,
        help_text="Read timeout in seconds for this route",
    )

    # Rate limiting
    rate_limit_enabled = models.BooleanField(default=False)
    rate_limit_rps = models.IntegerField(
        default=100,
        help_text="Requests per second allowed per client IP",
    )
    rate_limit_burst = models.IntegerField(
        default=200,
        help_text="How many requests may arrive above the rate before any are rejected",
    )

    # State
    is_active = models.BooleanField(default=True, db_index=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["domain", "priority", "match_value"]
        unique_together = [("domain", "match_type", "match_value")]

    def __str__(self) -> str:
        arrow = "→"
        return f"{self.domain.name}{self.match_value} {arrow} {self.backend.name}"

    @property
    def nginx_location(self) -> str:
        """Build the Nginx location directive string."""
        if self.match_type == self.MatchType.EXACT_PATH:
            return f"= {self.match_value}"
        if self.match_type == self.MatchType.REGEX:
            return f"~ {self.match_value}"
        return self.match_value

    @property
    def slug(self) -> str:
        """An identifier safe to embed in generated Nginx symbol names."""
        base = f"{self.domain_id}_{self.pk}"
        return f"rule_{base}"

    @property
    def has_header_routing(self) -> bool:
        return self.header_routes.filter(is_active=True).exists()


class ConfigDeployLog(models.Model):
    """
    Audit log for Nginx config deployments.

    Tracks who deployed what, when, and whether it succeeded.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        TESTING = "testing", "Testing"
        DEPLOYED = "deployed", "Deployed"
        FAILED = "failed", "Failed"
        ROLLED_BACK = "rolled_back", "Rolled Back"

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    config_snapshot = models.TextField(
        help_text="Full rendered Nginx config at time of deploy",
    )
    error_output = models.TextField(
        blank=True,
        default="",
        help_text="Output from nginx -t if the test failed",
    )
    deployed_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Deploy #{self.pk} [{self.status}] at {self.created_at}"


class HeaderRoute(models.Model):
    """
    A header-value override for a routing rule.

    The rule already decided *where* a path goes. This decides that one header
    value goes somewhere else instead — a canary pool, a tenant's dedicated
    instances, a maintenance stub.

    Rendered as an Nginx `map` feeding a variable `proxy_pass`. Nginx resolves
    that variable against declared upstream names before it considers DNS, so
    the keepalive pool survives; the per-request cost is one hash lookup.
    """

    rule = models.ForeignKey(
        RoutingRule,
        on_delete=models.CASCADE,
        related_name="header_routes",
    )
    backend = models.ForeignKey(
        "backends.Backend",
        on_delete=models.CASCADE,
        related_name="header_routes",
    )
    header_name = models.CharField(
        max_length=64,
        default="X-Route",
        help_text="Request header to switch on, e.g. X-Route",
    )
    header_value = models.CharField(
        max_length=255,
        help_text="Value that sends the request to this backend instead",
    )
    description = models.CharField(max_length=255, blank=True, default="")

    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rule", "header_name", "header_value"]
        unique_together = [("rule", "header_name", "header_value")]

    def __str__(self) -> str:
        return f"{self.header_name}: {self.header_value} -> {self.backend.name}"

    @property
    def nginx_variable(self) -> str:
        """The `$http_*` variable Nginx exposes for this header."""
        return "$http_" + self.header_name.lower().replace("-", "_")
