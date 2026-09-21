"""Monitoring API views — everything the dashboard reads."""
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.monitoring.selectors import (
    backend_health_grid,
    cache_stats,
    dashboard_summary,
    traffic_series,
    traffic_stats,
    upstream_stats,
)


class DashboardSummaryApi(APIView):
    """
    GET /api/v1/monitoring/summary/  — counts for the dashboard header
    """

    def get(self, request):
        return Response(dashboard_summary())


class TrafficApi(APIView):
    """
    GET /api/v1/monitoring/traffic/  — live per-domain request counters
    """

    def get(self, request):
        return Response(traffic_stats())


class CacheApi(APIView):
    """
    GET /api/v1/monitoring/cache/  — hit ratio, disk use, status breakdown
    """

    def get(self, request):
        return Response(cache_stats())


class UpstreamApi(APIView):
    """
    GET /api/v1/monitoring/upstreams/  — per-instance traffic as Nginx saw it
    """

    def get(self, request):
        return Response(upstream_stats())


class HealthGridApi(APIView):
    """
    GET /api/v1/monitoring/health/  — per-instance probe results
    """

    def get(self, request):
        return Response({"backends": backend_health_grid()})


class TimeSeriesApi(APIView):
    """
    GET /api/v1/monitoring/series/?minutes=60&step=30s  — chart data
    """

    def get(self, request):
        try:
            minutes = min(int(request.query_params.get("minutes", 60)), 60 * 24 * 7)
        except ValueError:
            minutes = 60
        step = request.query_params.get("step", "30s")
        return Response(traffic_series(minutes=minutes, step=step))


class OverviewApi(APIView):
    """
    GET /api/v1/monitoring/overview/  — one call for the whole landing page

    The dashboard would otherwise open with five parallel requests, each with
    its own spinner. One response means the page paints once.
    """

    def get(self, request):
        return Response({
            "summary": dashboard_summary(),
            "traffic": traffic_stats(),
            "cache": cache_stats(),
            "upstreams": upstream_stats(),
            "health": backend_health_grid(),
        })
