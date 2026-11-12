"""
A record of every backup taken, and of every attempt to prove one works.

A backup nobody has restored is a hypothesis. The verification half of this
model exists because the failure that matters is not a backup that errors --
that one is visible -- but a backup that completes for months and turns out to
be unrestorable on the day it is needed.
"""
from django.db import models


class BackupRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        SCHEDULE = "schedule", "Scheduled"
        MANUAL = "manual", "Started by an operator"
        COMMAND = "command", "Command line"

    class Verification(models.TextChoices):
        PENDING = "pending", "Not checked yet"
        PASSED = "passed", "Restored into a scratch database"
        FAILED = "failed", "Could not be restored"

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True
    )
    trigger = models.CharField(
        max_length=16, choices=Trigger.choices, default=Trigger.SCHEDULE
    )
    started_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="backups_started",
    )

    filename = models.CharField(max_length=255, blank=True, default="")
    size_bytes = models.BigIntegerField(default=0)
    # Of the archive as written, so bit rot on disk is detectable without
    # decrypting or unpacking anything.
    sha256 = models.CharField(max_length=64, blank=True, default="")
    is_encrypted = models.BooleanField(default=False)

    # What went into it, for a restore that only needs one part back.
    components = models.JSONField(default=dict, blank=True)

    verification = models.CharField(
        max_length=16, choices=Verification.choices, default=Verification.PENDING,
        db_index=True,
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_detail = models.TextField(blank=True, default="")

    error = models.TextField(blank=True, default="")

    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["-started_at", "status"])]

    def __str__(self) -> str:
        return f"{self.filename or 'backup'} ({self.status})"

    @property
    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def is_restorable(self) -> bool:
        """
        Complete and proven, not merely complete.

        The dashboard counts these rather than completed runs, because a run
        that finished is only a claim until something has read it back.
        """
        return (
            self.status == self.Status.COMPLETE
            and self.verification == self.Verification.PASSED
        )
