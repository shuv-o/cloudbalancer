"""Monitoring API views — everything the dashboard reads."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.domains.models import Domain
from apps.monitoring.selectors import (
    SERIES_RANGES,
    SeriesFilterError,
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
    GET /api/v1/monitoring/series/?minutes=1440&domain=example.com  — chart data

    The step is not a parameter. It follows from the range: Prometheus refuses
    a range query asking for more than 11,000 points, so a caller free to pick
    both would mostly be free to pick a 422. The step actually used comes back
    in the response, for the chart to label itself with.
    """

    def get(self, request):
        try:
            minutes = int(request.query_params.get("minutes", 60))
        except (TypeError, ValueError):
            minutes = 60

        domain = (request.query_params.get("domain") or "").strip()

        try:
            return Response(traffic_series(minutes=minutes, host=domain or None))
        except SeriesFilterError as exc:
            # A domain that cannot be charted is a bad request, not an empty
            # one. Returning no data would be indistinguishable from a domain
            # that is simply quiet.
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class ChartableDomainsApi(APIView):
    """
    GET /api/v1/monitoring/series/domains/  — what the domain picker offers

    Every configured domain, whether or not it has traffic yet. A domain
    missing from the list because nobody has visited it reads as a domain that
    was never set up.
    """

    def get(self, request):
        return Response({
            "domains": list(
                Domain.objects.order_by("name").values_list("name", flat=True)
            ),
            "ranges": SERIES_RANGES,
        })


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
