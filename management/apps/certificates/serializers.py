"""Certificate serializers."""
from rest_framework import serializers

from apps.certificates.models import AcmeAccount, Certificate


class AcmeAccountInputSerializer(serializers.Serializer):
    email = serializers.EmailField()
    directory_url = serializers.ChoiceField(
        choices=AcmeAccount.Directory.choices,
        default=AcmeAccount.Directory.LETSENCRYPT,
    )
    agreed_to_tos = serializers.BooleanField(default=False)
    external_account_kid = serializers.CharField(required=False, allow_blank=True, default="")
    external_account_hmac = serializers.CharField(required=False, allow_blank=True, default="")
    is_default = serializers.BooleanField(required=False, default=False)


class AcmeAccountOutputSerializer(serializers.ModelSerializer):
    directory_label = serializers.CharField(source="get_directory_url_display", read_only=True)
    is_staging = serializers.BooleanField(read_only=True)
    certificate_count = serializers.IntegerField(source="certificates.count", read_only=True)

    class Meta:
        model = AcmeAccount
        fields = [
            "id", "email", "directory_url", "directory_label", "is_staging",
            "agreed_to_tos", "external_account_kid", "is_default",
            "registered_at", "certificate_count", "created_at", "updated_at",
        ]


class CertificateOutputSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField(source="domain.name", read_only=True)
    issuer_label = serializers.CharField(source="get_issuer_display", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    days_until_expiry = serializers.IntegerField(read_only=True)
    is_due_for_renewal = serializers.BooleanField(read_only=True)
    acme_account_email = serializers.CharField(source="acme_account.email", read_only=True, default=None)

    class Meta:
        model = Certificate
        fields = [
            "id", "domain", "domain_name",
            "issuer", "issuer_label", "status", "status_label",
            "acme_account", "acme_account_email",
            "subject_common_name", "san_domains",
            "serial_number", "fingerprint_sha256",
            "not_before", "not_after", "days_until_expiry", "is_due_for_renewal",
            "auto_renew", "renew_before_days",
            "last_issued_at", "last_attempt_at", "last_error", "attempt_count",
            "created_at", "updated_at",
        ]


class CertificateRequestSerializer(serializers.Serializer):
    """Input for requesting an ACME certificate for a domain."""
    domain_id = serializers.IntegerField()
    acme_account_id = serializers.IntegerField(required=False, allow_null=True)
    extra_domains = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
        help_text="Additional SANs to include, e.g. a www alias",
    )
    force = serializers.BooleanField(required=False, default=False)


class CertificateSelfSignedSerializer(serializers.Serializer):
    domain_id = serializers.IntegerField()
    valid_days = serializers.IntegerField(required=False, default=825, min_value=1, max_value=3650)


class CertificateImportSerializer(serializers.Serializer):
    domain_id = serializers.IntegerField()
    cert_pem = serializers.CharField(trim_whitespace=False)
    key_pem = serializers.CharField(trim_whitespace=False)


class CertificateUpdateSerializer(serializers.Serializer):
    auto_renew = serializers.BooleanField(required=False)
    renew_before_days = serializers.IntegerField(required=False, min_value=1, max_value=89)
