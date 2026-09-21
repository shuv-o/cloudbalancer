"""Domain serializers — input validation and output formatting."""
from rest_framework import serializers

from apps.domains.models import Domain


class DomainInputSerializer(serializers.Serializer):
    """Validates input for creating or updating a domain."""
    name = serializers.CharField(max_length=253)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    ssl_enabled = serializers.BooleanField(required=False, default=False)
    ssl_cert_path = serializers.CharField(required=False, default="", allow_blank=True)
    ssl_key_path = serializers.CharField(required=False, default="", allow_blank=True)
    force_ssl_redirect = serializers.BooleanField(required=False, default=True)
    is_active = serializers.BooleanField(required=False, default=True)


class DomainCertificateSummarySerializer(serializers.Serializer):
    """The slice of certificate state a domain row needs to show."""
    id = serializers.IntegerField()
    issuer = serializers.CharField()
    issuer_label = serializers.CharField(source="get_issuer_display")
    status = serializers.CharField()
    not_after = serializers.DateTimeField()
    days_until_expiry = serializers.IntegerField()
    auto_renew = serializers.BooleanField()
    last_error = serializers.CharField()


class DomainOutputSerializer(serializers.ModelSerializer):
    """Formats domain output for API responses."""
    rule_count = serializers.IntegerField(read_only=True, required=False)
    active_rule_count = serializers.IntegerField(read_only=True, required=False)
    certificate = serializers.SerializerMethodField()

    class Meta:
        model = Domain
        fields = [
            "id", "name", "description",
            "ssl_enabled", "ssl_cert_path", "ssl_key_path", "force_ssl_redirect",
            "is_active", "created_at", "updated_at",
            "rule_count", "active_rule_count", "certificate",
        ]

    def get_certificate(self, obj) -> dict | None:
        certificate = getattr(obj, "certificate", None)
        if certificate is None:
            return None
        return DomainCertificateSummarySerializer(certificate).data
