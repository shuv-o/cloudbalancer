"""Backend serializers."""
from rest_framework import serializers
from apps.backends.models import Backend, BackendInstance


class BackendInstanceInputSerializer(serializers.Serializer):
    address = serializers.IPAddressField()
    port = serializers.IntegerField(default=8080, min_value=1, max_value=65535)
    weight = serializers.IntegerField(default=1, min_value=1)
    max_fails = serializers.IntegerField(default=3, min_value=0)
    fail_timeout = serializers.IntegerField(default=10, min_value=0)
    is_active = serializers.BooleanField(required=False, default=True)


class BackendInstanceOutputSerializer(serializers.ModelSerializer):
    state = serializers.CharField(read_only=True)

    class Meta:
        model = BackendInstance
        fields = [
            "id", "backend", "address", "port", "weight",
            "max_fails", "fail_timeout",
            "is_healthy", "is_draining", "state",
            "consecutive_failures", "consecutive_successes",
            "last_health_check",
            "last_health_status_code", "last_health_response_ms",
            "is_active", "created_at", "updated_at",
        ]


class BackendInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, default="")
    lb_method = serializers.ChoiceField(
        choices=Backend.LBMethod.choices,
        default=Backend.LBMethod.ROUND_ROBIN,
    )
    keepalive_connections = serializers.IntegerField(default=32, min_value=1)
    keepalive_requests = serializers.IntegerField(default=1000, min_value=1)
    keepalive_timeout = serializers.IntegerField(default=60, min_value=1)
    drain_unhealthy = serializers.BooleanField(required=False, default=False)
    unhealthy_threshold = serializers.IntegerField(default=3, min_value=1)
    healthy_threshold = serializers.IntegerField(default=2, min_value=1)
    health_check_enabled = serializers.BooleanField(required=False, default=True)
    health_check_path = serializers.CharField(required=False, default="/health")
    health_check_interval = serializers.IntegerField(default=10, min_value=1)
    health_check_timeout = serializers.IntegerField(default=5, min_value=1)
    is_active = serializers.BooleanField(required=False, default=True)


class BackendOutputSerializer(serializers.ModelSerializer):
    instances = BackendInstanceOutputSerializer(many=True, read_only=True)
    healthy_instance_count = serializers.IntegerField(read_only=True)
    total_instance_count = serializers.IntegerField(read_only=True)
    serving_instance_count = serializers.IntegerField(read_only=True)
    lb_method_label = serializers.CharField(source="get_lb_method_display", read_only=True)

    class Meta:
        model = Backend
        fields = [
            "id", "name", "description", "lb_method", "lb_method_label",
            "keepalive_connections", "keepalive_requests", "keepalive_timeout",
            "drain_unhealthy", "unhealthy_threshold", "healthy_threshold",
            "health_check_enabled", "health_check_path",
            "health_check_interval", "health_check_timeout",
            "is_active", "created_at", "updated_at",
            "instances", "healthy_instance_count", "total_instance_count",
            "serving_instance_count",
        ]
