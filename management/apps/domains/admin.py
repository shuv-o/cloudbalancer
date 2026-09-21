from django.contrib import admin

from apps.domains.models import Domain


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ["name", "ssl_enabled", "force_ssl_redirect", "is_active", "updated_at"]
    list_filter = ["ssl_enabled", "is_active"]
    search_fields = ["name", "description"]
