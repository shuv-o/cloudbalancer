from django.urls import path

from apps.monitoring.apis import (
    CacheApi,
    DashboardSummaryApi,
    HealthGridApi,
    OverviewApi,
    TimeSeriesApi,
    TrafficApi,
    UpstreamApi,
)

urlpatterns = [
    path("overview/", OverviewApi.as_view(), name="monitoring-overview"),
    path("summary/", DashboardSummaryApi.as_view(), name="monitoring-summary"),
    path("traffic/", TrafficApi.as_view(), name="monitoring-traffic"),
    path("cache/", CacheApi.as_view(), name="monitoring-cache"),
    path("upstreams/", UpstreamApi.as_view(), name="monitoring-upstreams"),
    path("health/", HealthGridApi.as_view(), name="monitoring-health"),
    path("series/", TimeSeriesApi.as_view(), name="monitoring-series"),
]
