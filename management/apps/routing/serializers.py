"""Routing serializers."""
from rest_framework import serializers

from apps.routing.models import ConfigDeployLog, HeaderRoute, RoutingRule


class HeaderRouteInputSerializer(serializers.Serializer):
    backend_id = serializers.IntegerField()
    header_name = serializers.CharField(max_length=64, default="X-Route")
    header_value = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    is_active = serializers.BooleanField(required=False, default=True)


class HeaderRouteOutputSerializer(serializers.ModelSerializer):
    backend_name = serializers.CharField(source="backend.name", read_only=True)

    class Meta:
        model = HeaderRoute
        fields = [
            "id", "rule", "backend", "backend_name",
            "header_name", "header_value", "description",
            "is_active", "created_at", "updated_at",
        ]


class RoutingRuleInputSerializer(serializers.Serializer):
    domain_id = serializers.IntegerField()
    backend_id = serializers.IntegerField()
    match_type = serializers.ChoiceField(
        choices=RoutingRule.MatchType.choices,
        default=RoutingRule.MatchType.PATH_PREFIX,
    )
    match_value = serializers.CharField(max_length=255, default="/")
    priority = serializers.IntegerField(default=100)

    cache_enabled = serializers.BooleanField(required=False, default=False)
    cache_ttl = serializers.IntegerField(required=False, default=600, min_value=0)
    cache_bypass_auth = serializers.BooleanField(required=False, default=True)
    cache_min_uses = serializers.IntegerField(required=False, default=1, min_value=1)
    cache_ignore_query_string = serializers.BooleanField(required=False, default=False)
    cache_key_headers = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, default=list
    )
    cache_ignore_upstream_control = serializers.BooleanField(required=False, default=False)
    cache_allow_authenticated = serializers.BooleanField(required=False, default=False)

    strip_prefix = serializers.BooleanField(required=False, default=False)
    custom_headers = serializers.DictField(
        child=serializers.CharField(), required=False, default=dict
    )
    proxy_buffering = serializers.BooleanField(required=False, default=True)
    proxy_read_timeout = serializers.IntegerField(required=False, default=60, min_value=1)

    rate_limit_enabled = serializers.BooleanField(required=False, default=False)
    rate_limit_rps = serializers.IntegerField(required=False, default=100, min_value=1)
    rate_limit_burst = serializers.IntegerField(required=False, default=200, min_value=0)

    is_active = serializers.BooleanField(required=False, default=True)


class RoutingRuleOutputSerializer(serializers.ModelSerializer):
    domain_name = serializers.CharField(source="domain.name", read_only=True)
    backend_name = serializers.CharField(source="backend.name", read_only=True)
    match_type_label = serializers.CharField(source="get_match_type_display", read_only=True)
    nginx_location = serializers.CharField(read_only=True)
    header_routes = HeaderRouteOutputSerializer(many=True, read_only=True)

    class Meta:
        model = RoutingRule
        fields = [
            "id", "domain", "domain_name", "backend", "backend_name",
            "match_type", "match_type_label", "match_value", "nginx_location", "priority",
            "cache_enabled", "cache_ttl", "cache_bypass_auth", "cache_min_uses",
            "cache_ignore_query_string", "cache_key_headers",
            "cache_ignore_upstream_control", "cache_allow_authenticated",
            "strip_prefix", "custom_headers",
            "proxy_buffering", "proxy_read_timeout",
            "rate_limit_enabled", "rate_limit_rps", "rate_limit_burst",
            "is_active", "created_at", "updated_at",
            "header_routes",
        ]


class ConfigDeployLogOutputSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    deployed_by_username = serializers.CharField(
        source="deployed_by.username", read_only=True, default=None
    )
    duration_ms = serializers.SerializerMethodField()

    class Meta:
        model = ConfigDeployLog
        fields = [
            "id", "status", "status_label", "error_output",
            "deployed_by", "deployed_by_username",
            "created_at", "completed_at", "duration_ms",
        ]

    def get_duration_ms(self, obj) -> float | None:
        if obj.completed_at is None:
            return None
        return round((obj.completed_at - obj.created_at).total_seconds() * 1000, 1)


class ConfigDeployLogDetailSerializer(ConfigDeployLogOutputSerializer):
    """Adds the rendered config, which is too large for list responses."""

    class Meta(ConfigDeployLogOutputSerializer.Meta):
        fields = ConfigDeployLogOutputSerializer.Meta.fields + ["config_snapshot"]
