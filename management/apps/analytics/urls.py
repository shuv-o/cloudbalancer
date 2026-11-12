from django.urls import path

from apps.analytics.apis import (
    AnalyticsOverviewApi,
    ClientsApi,
    GeoipApi,
    GeographyApi,
    HeatmapApi,
    PathsApi,
)

urlpatterns = [
    path("", AnalyticsOverviewApi.as_view(), name="analytics-overview"),
    path("clients/", ClientsApi.as_view(), name="analytics-clients"),
    path("geography/", GeographyApi.as_view(), name="analytics-geography"),
    path("paths/", PathsApi.as_view(), name="analytics-paths"),
    path("heatmap/", HeatmapApi.as_view(), name="analytics-heatmap"),
    path("geoip/", GeoipApi.as_view(), name="analytics-geoip"),
]
