"""
One place where something being wrong turns into somebody being told.

There were two kinds of problem here and no path from either to a person.
Prometheus could see that the gateway was returning 5xx; the control plane
knew that a certificate expired in six days, that last night's backup failed
verification, that a deploy was rejected. Both were visible only to whoever
opened the panel and looked.

Both now arrive here. Prometheus alerts come in through an Alertmanager
webhook; control-plane conditions are raised directly by the code that
notices them. Having one model behind both is the point: one history to read
during an incident, one place to configure who hears about it, and one answer
to "were we told?".

Alerts are keyed by a fingerprint rather than created per occurrence. A
backend that flaps every thirty seconds is one problem, and a history that
records it three hundred times is a history nobody reads.
"""
from django.db import models
from django.utils import timezone


class Severity(models.TextChoices):
    INFO = "info", "Information"
    WARNING = "warning", "Warning"
    CRITICAL = "critical", "Critical"


# Ascending, for "notify me about this and anything worse".
SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


class NotificationChannel(models.Model):
    """
    Somewhere to send an alert.

    Configuration is a JSON blob rather than columns per kind, because the
    kinds have nothing in common -- an address, a URL, a token -- and a table
    with every field for every kind is mostly nulls and an invitation to send
    a Slack alert to an email address.
    """

    class Kind(models.TextChoices):
        EMAIL = "email", "Email"
        WEBHOOK = "webhook", "Webhook"
        SLACK = "slack", "Slack or Mattermost"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    config = models.JSONField(default=dict)

    is_enabled = models.BooleanField(default=True)
    min_severity = models.CharField(
        max_length=16,
        choices=Severity.choices,
        default=Severity.WARNING,
        help_text="This severity and anything worse. Information is noisy by design.",
    )

    # Set when a delivery fails, cleared when one succeeds. A channel that has
    # been silently failing is worse than no channel: it is a channel somebody
    # is relying on.
    last_error = models.TextField(blank=True, default="")
    last_success_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.kind})"

    @property
    def is_healthy(self) -> bool:
        return self.consecutive_failures == 0

    def wants(self, severity: str) -> bool:
        return (
            self.is_enabled
            and SEVERITY_RANK[Severity(severity)] >= SEVERITY_RANK[Severity(self.min_severity)]
        )

    def describe_target(self) -> str:
        """The destination, for the panel, with nothing secret in it."""
        if self.kind == self.Kind.EMAIL:
            return ", ".join(self.config.get("to", []))
        url = self.config.get("url", "")
        # A Slack webhook URL is a credential. Showing the host is enough to
        # tell two channels apart, and the path is the part that lets anyone
        # holding it post as you.
        if "://" in url:
            host = url.split("://", 1)[1].split("/", 1)[0]
            return f"{host}/…"
        return url


class Alert(models.Model):
    """One problem, however many times it has been observed."""

    class Status(models.TextChoices):
        FIRING = "firing", "Firing"
        RESOLVED = "resolved", "Resolved"

    class Source(models.TextChoices):
        PROMETHEUS = "prometheus", "Prometheus"
        CONTROL_PLANE = "control_plane", "Control plane"

    # Stable across occurrences: the same problem recurring updates this row
    # rather than adding another.
    fingerprint = models.CharField(max_length=128, unique=True, db_index=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices, db_index=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.FIRING, db_index=True
    )

    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default="")
    # What to do about it. Written where the alert is raised, because that is
    # where somebody knew.
    runbook = models.TextField(blank=True, default="")
    labels = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    occurrences = models.PositiveIntegerField(default=1)
    # When this alert was last sent anywhere, so a problem lasting a week does
    # not send a message every minute for a week.
    last_notified_at = models.DateTimeField(null=True, blank=True)

    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="alerts_acknowledged",
    )

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["status", "-started_at"]),
            models.Index(fields=["severity", "status"]),
        ]

    def __str__(self) -> str:
        return f"[{self.severity}] {self.title}"

    @property
    def is_acknowledged(self) -> bool:
        return self.acknowledged_at is not None

    @property
    def duration_seconds(self) -> float:
        end = self.resolved_at or timezone.now()
        return (end - self.started_at).total_seconds()


class NotificationAttempt(models.Model):
    """
    Every send, successful or not.

    Kept because the question after an incident is rarely "what went wrong"
    and often "why did nobody know", and the two answers -- nothing was sent,
    or something was sent and failed -- lead to very different fixes.
    """

    alert = models.ForeignKey(Alert, on_delete=models.CASCADE, related_name="attempts")
    channel = models.ForeignKey(
        NotificationChannel, on_delete=models.CASCADE, related_name="attempts"
    )
    succeeded = models.BooleanField()
    detail = models.TextField(blank=True, default="")
    # Resolution notices go out too, and are worth telling apart from the
    # original when reading back.
    is_resolution = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.channel.name}: {'sent' if self.succeeded else 'failed'}"
