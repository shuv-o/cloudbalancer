from django.urls import path

from apps.gateway.apis import (
    GatewayCachePurgeApi,
    GatewayCachePurgeUrlApi,
    GatewayConfigPreviewApi,
    GatewayDeployApi,
    GatewayStatusApi,
)

urlpatterns = [
    path("status/", GatewayStatusApi.as_view(), name="gateway-status"),
    path("deploy/", GatewayDeployApi.as_view(), name="gateway-deploy"),
    path("config/", GatewayConfigPreviewApi.as_view(), name="gateway-config-preview"),
    path("cache/purge/", GatewayCachePurgeApi.as_view(), name="gateway-cache-purge"),
    path("cache/purge-url/", GatewayCachePurgeUrlApi.as_view(), name="gateway-cache-purge-url"),
]
