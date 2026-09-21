"""Security serializers."""
import ipaddress

from rest_framework import serializers

from apps.security.models import (
    AuditEvent,
    BlockedAddress,
    LoginAttempt,
    PanelAccessPolicy,
    TrafficProtectionPolicy,
)


class CidrListField(serializers.ListField):
    child = serializers.CharField()

    def to_internal_value(self, data):
        entries = super().to_internal_value(data)
        cleaned = []
        for entry in entries:
            text = entry.strip()
            if not text:
                continue
            try:
                cleaned.append(str(ipaddress.ip_network(text, strict=False)))
            except ValueError:
                raise serializers.ValidationError(
                    f"'{text}' is not an address or range. Use a single address like "
                    "203.0.113.4 or a range like 203.0.113.0/24."
                )
        return cleaned


class PolicyInputSerializer(serializers.Serializer):
    panel_domain = serializers.CharField(max_length=253, required=False, allow_blank=True)
    is_published = serializers.BooleanField(required=False)
    ip_allowlist = CidrListField(required=False)
    require_mtls = serializers.BooleanField(required=False)
    mtls_ca_path = serializers.CharField(max_length=512, required=False, allow_blank=True)
    hsts_seconds = serializers.IntegerField(required=False, min_value=0, max_value=63072000)
    require_totp = serializers.BooleanField(required=False)
    login_rate_per_minute = serializers.IntegerField(required=False, min_value=1, max_value=600)
    api_rate_per_second = serializers.IntegerField(required=False, min_value=1, max_value=1000)
    lockout_threshold = serializers.IntegerField(required=False, min_value=0, max_value=100)
    lockout_minutes = serializers.IntegerField(required=False, min_value=1, max_value=1440)
    session_idle_minutes = serializers.IntegerField(required=False, min_value=5, max_value=1440)
    session_max_hours = serializers.IntegerField(required=False, min_value=1, max_value=168)
    expose_django_admin = serializers.BooleanField(required=False)


class PolicyOutputSerializer(serializers.ModelSerializer):
    warnings = serializers.ListField(source="exposure_warnings", read_only=True)
    normalised_allowlist = serializers.ListField(read_only=True)

    class Meta:
        model = PanelAccessPolicy
        fields = [
            "panel_domain", "is_published",
            "ip_allowlist", "normalised_allowlist",
            "require_mtls", "mtls_ca_path", "hsts_seconds",
            "require_totp",
            "login_rate_per_minute", "api_rate_per_second",
            "lockout_threshold", "lockout_minutes",
            "session_idle_minutes", "session_max_hours",
            "expose_django_admin",
            "warnings", "updated_at",
        ]


class AuditEventOutputSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = [
            "id", "actor_name", "action", "path", "status_code",
            "ip_address", "summary", "created_at",
        ]


class LoginAttemptOutputSerializer(serializers.ModelSerializer):
    outcome_label = serializers.CharField(source="get_outcome_display", read_only=True)

    class Meta:
        model = LoginAttempt
        fields = [
            "id", "username", "ip_address", "outcome", "outcome_label",
            "user_agent", "created_at",
        ]


class TotpConfirmSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=16)


class TotpDisableSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False)
    code = serializers.CharField(max_length=16)


class TotpResetSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()


class ProtectionInputSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(required=False)
    per_ip_connections = serializers.IntegerField(required=False, min_value=1, max_value=10000)
    per_ip_requests_per_second = serializers.IntegerField(
        required=False, min_value=1, max_value=100000
    )
    per_ip_burst = serializers.IntegerField(required=False, min_value=0, max_value=100000)
    client_header_timeout = serializers.IntegerField(required=False, min_value=1, max_value=300)
    client_body_timeout = serializers.IntegerField(required=False, min_value=1, max_value=300)
    denylist_enabled = serializers.BooleanField(required=False)


class ProtectionOutputSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrafficProtectionPolicy
        fields = [
            "enabled",
            "per_ip_connections", "per_ip_requests_per_second", "per_ip_burst",
            "client_header_timeout", "client_body_timeout",
            "denylist_enabled", "updated_at",
        ]


class BlockedAddressInputSerializer(serializers.Serializer):
    cidr = serializers.CharField(max_length=64)
    reason = serializers.ChoiceField(
        choices=BlockedAddress.Reason.choices, default=BlockedAddress.Reason.MANUAL
    )
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    minutes = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=525600,
        help_text="Leave empty for a block that does not expire",
    )


class BlockedAddressOutputSerializer(serializers.ModelSerializer):
    reason_label = serializers.CharField(source="get_reason_display", read_only=True)
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )
    is_active = serializers.BooleanField(read_only=True)

    class Meta:
        model = BlockedAddress
        fields = [
            "id", "cidr", "reason", "reason_label", "note",
            "expires_at", "is_active",
            "created_by_username", "created_at",
        ]
