"""
Traffic analytics API.

Aggregates are viewer-readable: knowing which countries and paths a gateway
serves is part of running it, and none of it identifies anybody.

Complete client addresses are not. They are personal data, kept for thirty
days to investigate abuse, and that is an operator's job rather than
something visible to anyone who can open a dashboard. The split is on the
endpoint, not on a field, so there is no path by which a viewer receives one.
"""
import logging

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.analytics import geo, selectors
from apps.security.roles import IsAdmin, Role

logger = logging.getLogger(__name__)


def _params(request) -> dict:
    try:
        days = int(request.query_params.get("days", 7))
    except (TypeError, ValueError):
        days = 7
    return {"days": days, "domain": (request.query_params.get("domain") or "").strip() or None}


class AnalyticsOverviewApi(APIView):
    """
    GET /api/v1/analytics/?days=7&domain=  — the header, and what can be asked
    """
    required_role = Role.VIEWER

    def get(self, request):
        return Response({
            "summary": selectors.summary(**_params(request)),
            "agents": selectors.agents(**_params(request)),
            "domains": selectors.domains(),
            "ranges": selectors.RANGES,
            "geoip": geo.available(),
        })


class ClientsApi(APIView):
    """
    GET /api/v1/analytics/clients/  — the busiest addresses, and where they are

    Operator and above. These are complete IP addresses.
    """
    required_role = Role.OPERATOR

    def get(self, request):
        try:
            limit = min(int(request.query_params.get("limit", 50)), 500)
        except (TypeError, ValueError):
            limit = 50
        return Response(selectors.top_clients(limit=limit, **_params(request)))


class GeographyApi(APIView):
    """
    GET /api/v1/analytics/geography/  — traffic by country and by network
    """
    required_role = Role.VIEWER

    def get(self, request):
        return Response({
            **selectors.geography(**_params(request)),
            "attribution": geo.available()["attribution"],
        })


class PathsApi(APIView):
    """
    GET /api/v1/analytics/paths/  — busiest, slowest and most broken
    """
    required_role = Role.VIEWER

    def get(self, request):
        return Response(selectors.paths(**_params(request)))


class HeatmapApi(APIView):
    """
    GET /api/v1/analytics/heatmap/?days=28  — requests by weekday and hour
    """
    required_role = Role.VIEWER

    def get(self, request):
        params = _params(request)
        params["days"] = params["days"] if params["days"] > 7 else 28
        return Response(selectors.heatmap(**params))


class GeoipApi(APIView):
    """
    GET  /api/v1/analytics/geoip/  — which databases are present
    POST /api/v1/analytics/geoip/  — fetch or refresh them

    Admin: it writes to disk and reaches an external service.
    """
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(geo.available())

    def post(self, request):
        from apps.analytics.tasks import update_geoip

        update_geoip.delay()
        return Response({
            "started": True,
            "note": "Downloading in the background. Existing rows keep the "
                    "location they were written with; new traffic gets the "
                    "refreshed data.",
        })
