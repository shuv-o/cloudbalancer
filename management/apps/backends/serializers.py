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
    # The form sends "" for an empty optional field, so rejecting blank
    # turns "I left the notes empty" into a 400.
    description = serializers.CharField(
        required=False, allow_blank=True, default=""
    )
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
    # Blank is not a usable path, but clearing the field should fall back to
    # the default rather than fail -- health checking is optional, and the
    # field is only visible when it is switched on.
    health_check_path = serializers.CharField(
        required=False, allow_blank=True, default="/health"
    )

    def validate_health_check_path(self, value: str) -> str:
        return value.strip() or "/health"
    health_check_interval = serializers.IntegerField(default=10, min_value=1)
    health_check_timeout = serializers.IntegerField(default=5, min_value=1)
    is_active = serializers.BooleanField(required=False, default=True)


class BackendOutputSerializer(serializers.ModelSerializer):
    instances = BackendInstanceOutputSerializer(many=True, read_only=True)
    healthy_instance_count = serializers.SerializerMethodField()
    total_instance_count = serializers.SerializerMethodField()
    serving_instance_count = serializers.SerializerMethodField()
    lb_method_label = serializers.CharField(source="get_lb_method_display", read_only=True)

    @staticmethod
    def _count(obj, annotated: str, prop: str) -> int:
        """
        Prefer the annotation the list query provides; fall back to the model
        property for a single object, where one extra query costs nothing.

        Checked against None rather than truthiness, or a genuine count of
        zero would silently fall through to the property.
        """
        value = getattr(obj, annotated, None)
        return value if value is not None else getattr(obj, prop)

    def get_healthy_instance_count(self, obj) -> int:
        return self._count(obj, "healthy_count", "healthy_instance_count")

    def get_total_instance_count(self, obj) -> int:
        return self._count(obj, "total_count", "total_instance_count")

    def get_serving_instance_count(self, obj) -> int:
        return self._count(obj, "serving_count", "serving_instance_count")

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
