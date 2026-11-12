"""Backup serializers."""
from rest_framework import serializers

from apps.backups.models import BackupRun


class BackupRunOutputSerializer(serializers.ModelSerializer):
    started_by_username = serializers.CharField(
        source="started_by.username", default=None, read_only=True
    )
    duration_seconds = serializers.FloatField(read_only=True)
    is_restorable = serializers.BooleanField(read_only=True)
    trigger_label = serializers.CharField(source="get_trigger_display", read_only=True)
    verification_label = serializers.CharField(
        source="get_verification_display", read_only=True
    )

    class Meta:
        model = BackupRun
        fields = [
            "id", "status", "trigger", "trigger_label", "started_by_username",
            "filename", "size_bytes", "sha256", "is_encrypted", "components",
            "verification", "verification_label", "verified_at",
            "verification_detail", "error", "started_at", "finished_at",
            "duration_seconds", "is_restorable",
        ]
