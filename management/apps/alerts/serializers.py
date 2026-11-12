"""Alert serializers."""
from rest_framework import serializers

from apps.alerts.models import (
    Alert,
    NotificationAttempt,
    NotificationChannel,
    Severity,
)


class AlertOutputSerializer(serializers.ModelSerializer):
    severity_label = serializers.CharField(source="get_severity_display", read_only=True)
    source_label = serializers.CharField(source="get_source_display", read_only=True)
    acknowledged_by_username = serializers.CharField(
        source="acknowledged_by.username", default=None, read_only=True
    )
    duration_seconds = serializers.FloatField(read_only=True)
    is_acknowledged = serializers.BooleanField(read_only=True)

    class Meta:
        model = Alert
        fields = [
            "id", "fingerprint", "source", "source_label", "severity",
            "severity_label", "status", "title", "body", "runbook", "labels",
            "started_at", "last_seen_at", "resolved_at", "occurrences",
            "last_notified_at", "acknowledged_at", "acknowledged_by_username",
            "is_acknowledged", "duration_seconds",
        ]


class ChannelOutputSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    target = serializers.CharField(source="describe_target", read_only=True)
    is_healthy = serializers.BooleanField(read_only=True)

    class Meta:
        model = NotificationChannel
        fields = [
            "id", "name", "kind", "kind_label", "target", "is_enabled",
            "min_severity", "is_healthy", "last_error", "last_success_at",
            "consecutive_failures", "created_at",
        ]


class ChannelInputSerializer(serializers.Serializer):
    """
    A channel to create or change.

    `config` is validated per kind rather than generically: the fields have
    nothing in common, and the failure worth preventing is a channel that
    saves cleanly and cannot possibly deliver.
    """

    name = serializers.CharField(max_length=100)
    kind = serializers.ChoiceField(choices=NotificationChannel.Kind.choices)
    config = serializers.DictField()
    is_enabled = serializers.BooleanField(default=True)
    min_severity = serializers.ChoiceField(
        choices=Severity.choices, default=Severity.WARNING
    )

    def validate(self, attrs):
        kind = attrs["kind"]
        config = attrs["config"]

        if kind == NotificationChannel.Kind.EMAIL:
            recipients = config.get("to")
            if not isinstance(recipients, list) or not recipients:
                raise serializers.ValidationError(
                    {"config": "An email channel needs a 'to' list with at least one address."}
                )
            for address in recipients:
                if "@" not in str(address):
                    raise serializers.ValidationError(
                        {"config": f"'{address}' is not an email address."}
                    )
        else:
            url = config.get("url", "")
            if not str(url).startswith(("http://", "https://")):
                raise serializers.ValidationError(
                    {"config": "This channel needs a 'url' starting with http:// or https://."}
                )

        return attrs


class AttemptOutputSerializer(serializers.ModelSerializer):
    channel_name = serializers.CharField(source="channel.name", read_only=True)
    alert_title = serializers.CharField(source="alert.title", read_only=True)

    class Meta:
        model = NotificationAttempt
        fields = [
            "id", "channel_name", "alert_title", "succeeded", "detail",
            "is_resolution", "created_at",
        ]
