from django.contrib import admin

from apps.routing.models import ConfigDeployLog, HeaderRoute, RoutingRule


class HeaderRouteInline(admin.TabularInline):
    model = HeaderRoute
    extra = 0


@admin.register(RoutingRule)
class RoutingRuleAdmin(admin.ModelAdmin):
    list_display = [
        "domain", "match_type", "match_value", "backend",
        "priority", "cache_enabled", "is_active",
    ]
    list_filter = ["match_type", "cache_enabled", "rate_limit_enabled", "is_active", "domain"]
    search_fields = ["match_value", "domain__name", "backend__name"]
    inlines = [HeaderRouteInline]


@admin.register(ConfigDeployLog)
class ConfigDeployLogAdmin(admin.ModelAdmin):
    list_display = ["id", "status", "deployed_by", "created_at", "completed_at"]
    list_filter = ["status"]
    readonly_fields = ["config_snapshot", "error_output", "created_at", "completed_at"]
