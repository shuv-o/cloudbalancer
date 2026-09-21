from django.contrib import admin

from apps.certificates.models import AcmeAccount, Certificate


@admin.register(AcmeAccount)
class AcmeAccountAdmin(admin.ModelAdmin):
    list_display = ["email", "directory_url", "is_default", "registered_at"]
    list_filter = ["directory_url", "is_default"]


@admin.register(Certificate)
class CertificateAdmin(admin.ModelAdmin):
    list_display = ["domain", "issuer", "status", "not_after", "auto_renew"]
    list_filter = ["issuer", "status", "auto_renew"]
    search_fields = ["domain__name", "subject_common_name"]
    readonly_fields = [
        "serial_number", "fingerprint_sha256", "not_before", "not_after",
        "san_domains", "last_issued_at", "last_attempt_at", "attempt_count",
    ]
