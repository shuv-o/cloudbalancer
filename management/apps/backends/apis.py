"""Backend API views."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.backends.models import Backend, BackendInstance
from apps.backends.selectors import (
    backend_list_with_stats,
    backend_get,
    instance_list,
    instance_get,
    backend_health_summary,
)
from apps.backends.serializers import (
    BackendInputSerializer,
    BackendOutputSerializer,
    BackendInstanceInputSerializer,
    BackendInstanceOutputSerializer,
)
from apps.backends.services import (
    backend_create,
    backend_update,
    backend_delete,
    instance_add,
    instance_update,
    instance_delete,
    health_check_backend,
)
from apps.gateway.tasks import request_config_deploy


class BackendListCreateApi(APIView):
    """
    GET  /api/v1/backends/           — List all backends
    POST /api/v1/backends/           — Create a new backend
    """

    def get(self, request):
        backends = backend_list_with_stats()
        serializer = BackendOutputSerializer(backends, many=True)
        return Response(serializer.data)

    def post(self, request):
        input_serializer = BackendInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        backend = backend_create(**input_serializer.validated_data)
        request_config_deploy(user_id=_user_id(request))
        output_serializer = BackendOutputSerializer(backend)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)


class BackendDetailApi(APIView):
    """
    GET    /api/v1/backends/<id>/    — Get backend details
    PUT    /api/v1/backends/<id>/    — Update a backend
    DELETE /api/v1/backends/<id>/    — Delete a backend
    """

    def get(self, request, backend_id):
        try:
            backend = backend_get(backend_id=backend_id)
        except Backend.DoesNotExist:
            return Response({"error": "Backend not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = BackendOutputSerializer(backend)
        return Response(serializer.data)

    def patch(self, request, backend_id):
        try:
            backend = backend_get(backend_id=backend_id)
        except Backend.DoesNotExist:
            return Response({"error": "Backend not found"}, status=status.HTTP_404_NOT_FOUND)

        input_serializer = BackendInputSerializer(data=request.data, partial=True)
        input_serializer.is_valid(raise_exception=True)
        backend = backend_update(backend=backend, data=input_serializer.validated_data)
        request_config_deploy(user_id=_user_id(request))
        output_serializer = BackendOutputSerializer(backend)
        return Response(output_serializer.data)

    put = patch

    def delete(self, request, backend_id):
        try:
            backend = backend_get(backend_id=backend_id)
        except Backend.DoesNotExist:
            return Response({"error": "Backend not found"}, status=status.HTTP_404_NOT_FOUND)
        backend_delete(backend=backend)
        request_config_deploy(user_id=_user_id(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


class BackendHealthCheckApi(APIView):
    """
    POST /api/v1/backends/<id>/health-check/  — Run health check now
    """

    def post(self, request, backend_id):
        try:
            backend = backend_get(backend_id=backend_id)
        except Backend.DoesNotExist:
            return Response({"error": "Backend not found"}, status=status.HTTP_404_NOT_FOUND)

        from apps.gateway.tasks import async_health_check_backend
        async_health_check_backend.delay(backend_id)
        return Response({"message": f"Health check queued for {backend.name}"}, status=status.HTTP_202_ACCEPTED)


class BackendHealthSummaryApi(APIView):
    """
    GET /api/v1/backends/health/    — Health summary for all backends
    """

    def get(self, request):
        return Response(backend_health_summary())


# ---------------------------------------------------------------------------
# Instance endpoints
# ---------------------------------------------------------------------------

class InstanceListCreateApi(APIView):
    """
    GET  /api/v1/backends/<id>/instances/     — List instances
    POST /api/v1/backends/<id>/instances/     — Add instance
    """

    def get(self, request, backend_id):
        instances = instance_list(backend_id=backend_id)
        serializer = BackendInstanceOutputSerializer(instances, many=True)
        return Response(serializer.data)

    def post(self, request, backend_id):
        try:
            backend = backend_get(backend_id=backend_id)
        except Backend.DoesNotExist:
            return Response({"error": "Backend not found"}, status=status.HTTP_404_NOT_FOUND)

        input_serializer = BackendInstanceInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        inst = instance_add(backend=backend, **input_serializer.validated_data)
        request_config_deploy(user_id=_user_id(request))
        output_serializer = BackendInstanceOutputSerializer(inst)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)


class InstanceDetailApi(APIView):
    """
    GET    /api/v1/backends/<backend_id>/instances/<id>/
    PUT    /api/v1/backends/<backend_id>/instances/<id>/
    DELETE /api/v1/backends/<backend_id>/instances/<id>/
    """

    def get(self, request, backend_id, instance_id):
        try:
            inst = instance_get(instance_id=instance_id)
        except BackendInstance.DoesNotExist:
            return Response({"error": "Instance not found"}, status=status.HTTP_404_NOT_FOUND)
        serializer = BackendInstanceOutputSerializer(inst)
        return Response(serializer.data)

    def patch(self, request, backend_id, instance_id):
        try:
            inst = instance_get(instance_id=instance_id)
        except BackendInstance.DoesNotExist:
            return Response({"error": "Instance not found"}, status=status.HTTP_404_NOT_FOUND)

        input_serializer = BackendInstanceInputSerializer(data=request.data, partial=True)
        input_serializer.is_valid(raise_exception=True)
        inst = instance_update(instance=inst, data=input_serializer.validated_data)
        request_config_deploy(user_id=_user_id(request))
        output_serializer = BackendInstanceOutputSerializer(inst)
        return Response(output_serializer.data)

    put = patch

    def delete(self, request, backend_id, instance_id):
        try:
            inst = instance_get(instance_id=instance_id)
        except BackendInstance.DoesNotExist:
            return Response({"error": "Instance not found"}, status=status.HTTP_404_NOT_FOUND)
        instance_delete(instance=inst)
        request_config_deploy(user_id=_user_id(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


def _user_id(request) -> int | None:
    user = getattr(request, "user", None)
    return user.pk if user is not None and user.is_authenticated else None
