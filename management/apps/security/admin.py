from django.contrib import admin

from apps.security.models import (
    AccountLock,
    AuditEvent,
    LoginAttempt,
    PanelAccessPolicy,
    TotpDevice,
)


@admin.register(PanelAccessPolicy)
class PanelAccessPolicyAdmin(admin.ModelAdmin):
    list_display = ["__str__", "require_totp", "require_mtls", "updated_at"]

    def has_add_permission(self, request):
        return not PanelAccessPolicy.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TotpDevice)
class TotpDeviceAdmin(admin.ModelAdmin):
    list_display = ["user", "confirmed_at", "last_used_at", "recovery_codes_remaining"]
    readonly_fields = ["secret", "recovery_codes", "last_used_step"]


@admin.register(LoginAttempt)
class LoginAttemptAdmin(admin.ModelAdmin):
    list_display = ["username", "ip_address", "outcome", "created_at"]
    list_filter = ["outcome"]
    search_fields = ["username", "ip_address"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AccountLock)
class AccountLockAdmin(admin.ModelAdmin):
    list_display = ["username", "locked_until", "failed_count", "last_ip"]


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "actor_name", "action", "path", "status_code", "ip_address"]
    list_filter = ["action", "status_code"]
    search_fields = ["actor_name", "path", "ip_address"]

    # Append-only: an audit trail someone can tidy up is not an audit trail.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
