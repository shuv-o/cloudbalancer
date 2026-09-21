"""Gateway API views — deploy control, config preview, cache operations."""
from django.conf import settings
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.gateway.services import (
    cache_purge_url,
    config_render,
    config_render_combined,
    nginx_test,
)
from apps.gateway.tasks import async_cache_purge_all, request_config_deploy
from apps.routing.models import ConfigDeployLog
from apps.routing.serializers import ConfigDeployLogOutputSerializer


class CachePurgeUrlSerializer(serializers.Serializer):
    host = serializers.CharField(max_length=253)
    uri = serializers.CharField(max_length=2048)
    scheme = serializers.ChoiceField(choices=["http", "https"], default="https")
    method = serializers.ChoiceField(choices=["GET", "HEAD"], default="GET")


class GatewayDeployApi(APIView):
    """
    POST /api/v1/gateway/deploy/  — push the current configuration live

    Coalesced: several calls inside the debounce window produce one reload.
    """

    def post(self, request):
        user = request.user if request.user.is_authenticated else None
        immediate = bool(request.data.get("immediate"))
        scheduled = request_config_deploy(
            user_id=user.pk if user else None,
            delay=0 if immediate else None,
        )
        return Response(
            {
                "message": (
                    "Deploying now."
                    if scheduled
                    else "A deploy is already queued; this change is included in it."
                ),
                "scheduled": scheduled,
                "debounce_seconds": settings.GATEWAY_DEPLOY_DEBOUNCE_SECONDS,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class GatewayConfigPreviewApi(APIView):
    """
    GET /api/v1/gateway/config/  — what a deploy would write, without writing it
    """

    def get(self, request):
        if request.query_params.get("format") == "combined":
            return Response({"config": config_render_combined()})
        return Response({"files": config_render()})


class GatewayStatusApi(APIView):
    """
    GET /api/v1/gateway/status/  — is the live configuration valid, and what
    happened on the last deploy
    """

    def get(self, request):
        test = nginx_test()
        last = ConfigDeployLog.objects.select_related("deployed_by").first()
        return Response({
            "config_valid": test.success,
            "config_test_output": test.output,
            "last_deploy": (
                ConfigDeployLogOutputSerializer(last).data if last else None
            ),
            "debounce_seconds": settings.GATEWAY_DEPLOY_DEBOUNCE_SECONDS,
        })


class GatewayCachePurgeApi(APIView):
    """
    POST /api/v1/gateway/cache/purge/  — empty the whole cache

    Every request misses until the cache refills, so the backends briefly take
    the full load. Purge a single URL instead when you know what changed.
    """

    def post(self, request):
        async_cache_purge_all.delay()
        return Response(
            {"message": "Emptying the cache. Expect a burst of backend traffic while it refills."},
            status=status.HTTP_202_ACCEPTED,
        )


class GatewayCachePurgeUrlApi(APIView):
    """
    POST /api/v1/gateway/cache/purge-url/  — drop one URL from the cache
    """

    def post(self, request):
        serializer = CachePurgeUrlSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = cache_purge_url(
            host=data["host"],
            uri=data["uri"],
            scheme=data["scheme"],
            method=data["method"],
        )
        return Response({
            "purged": result.purged,
            "keys": result.keys,
            "errors": result.errors,
            "message": (
                f"Removed {result.purged} cached "
                f"{'entry' if result.purged == 1 else 'entries'}."
                if result.purged
                else "That URL was not in the cache."
            ),
        })
