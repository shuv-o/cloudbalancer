from django.contrib import admin
from apps.backends.models import Backend, BackendInstance


class BackendInstanceInline(admin.TabularInline):
    model = BackendInstance
    extra = 1
    fields = ["address", "port", "weight", "is_healthy", "is_active", "last_health_check"]
    readonly_fields = ["is_healthy", "last_health_check"]


@admin.register(Backend)
class BackendAdmin(admin.ModelAdmin):
    list_display = ["name", "lb_method", "healthy_instance_count", "total_instance_count", "is_active"]
    list_filter = ["is_active", "lb_method"]
    search_fields = ["name"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [BackendInstanceInline]


@admin.register(BackendInstance)
class BackendInstanceAdmin(admin.ModelAdmin):
    list_display = ["backend", "address", "port", "weight", "is_healthy", "is_active"]
    list_filter = ["is_active", "is_healthy", "backend"]
    readonly_fields = ["last_health_check", "last_health_status_code", "last_health_response_ms"]
