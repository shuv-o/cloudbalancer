"""Routing API views."""
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.gateway.tasks import request_config_deploy
from apps.routing.models import ConfigDeployLog, HeaderRoute, RoutingRule
from apps.routing.selectors import (
    deploy_log_get,
    deploy_log_list,
    header_route_get,
    header_route_list,
    routing_stats,
    rule_get,
    rule_list,
)
from apps.routing.serializers import (
    ConfigDeployLogDetailSerializer,
    ConfigDeployLogOutputSerializer,
    HeaderRouteInputSerializer,
    HeaderRouteOutputSerializer,
    RoutingRuleInputSerializer,
    RoutingRuleOutputSerializer,
)
from apps.routing.services import (
    header_route_create,
    header_route_delete,
    header_route_update,
    rule_create,
    rule_delete,
    rule_update,
)


def _int_param(request, name: str):
    raw = request.query_params.get(name)
    return int(raw) if raw not in (None, "") else None


def _bool_param(request, name: str):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    return raw.lower() in ("1", "true", "yes")


class RoutingRuleListCreateApi(APIView):
    """
    GET  /api/v1/routing/rules/  — every rule, newest config state
    POST /api/v1/routing/rules/  — add a rule and push it to the gateway
    """

    def get(self, request):
        rules = rule_list(
            domain_id=_int_param(request, "domain_id"),
            backend_id=_int_param(request, "backend_id"),
            is_active=_bool_param(request, "is_active"),
        )
        return Response(RoutingRuleOutputSerializer(rules, many=True).data)

    def post(self, request):
        serializer = RoutingRuleInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            rule = rule_create(**serializer.validated_data)
        except DjangoValidationError as exc:
            return Response(exc.message_dict, status=status.HTTP_400_BAD_REQUEST)

        request_config_deploy(user_id=_user_id(request))
        return Response(
            RoutingRuleOutputSerializer(rule).data,
            status=status.HTTP_201_CREATED,
        )


class RoutingRuleDetailApi(APIView):
    """
    GET    /api/v1/routing/rules/<id>/
    PATCH  /api/v1/routing/rules/<id>/
    DELETE /api/v1/routing/rules/<id>/
    """

    def get(self, request, rule_id):
        try:
            rule = rule_get(rule_id=rule_id)
        except RoutingRule.DoesNotExist:
            return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(RoutingRuleOutputSerializer(rule).data)

    def patch(self, request, rule_id):
        try:
            rule = rule_get(rule_id=rule_id)
        except RoutingRule.DoesNotExist:
            return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = RoutingRuleInputSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            rule = rule_update(rule=rule, data=serializer.validated_data)
        except DjangoValidationError as exc:
            return Response(exc.message_dict, status=status.HTTP_400_BAD_REQUEST)

        request_config_deploy(user_id=_user_id(request))
        return Response(RoutingRuleOutputSerializer(rule).data)

    # PUT kept so existing clients that send a full object still work.
    put = patch

    def delete(self, request, rule_id):
        try:
            rule = rule_get(rule_id=rule_id)
        except RoutingRule.DoesNotExist:
            return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)

        rule_delete(rule=rule)
        request_config_deploy(user_id=_user_id(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


class HeaderRouteListCreateApi(APIView):
    """
    GET  /api/v1/routing/rules/<rule_id>/header-routes/
    POST /api/v1/routing/rules/<rule_id>/header-routes/
    """

    def get(self, request, rule_id):
        routes = header_route_list(rule_id=rule_id)
        return Response(HeaderRouteOutputSerializer(routes, many=True).data)

    def post(self, request, rule_id):
        if not RoutingRule.objects.filter(pk=rule_id).exists():
            return Response({"error": "Rule not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = HeaderRouteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        header_route = header_route_create(rule_id=rule_id, **serializer.validated_data)

        request_config_deploy(user_id=_user_id(request))
        return Response(
            HeaderRouteOutputSerializer(header_route).data,
            status=status.HTTP_201_CREATED,
        )


class HeaderRouteDetailApi(APIView):
    """
    PATCH  /api/v1/routing/header-routes/<id>/
    DELETE /api/v1/routing/header-routes/<id>/
    """

    def patch(self, request, header_route_id):
        try:
            header_route = header_route_get(header_route_id=header_route_id)
        except HeaderRoute.DoesNotExist:
            return Response({"error": "Header route not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = HeaderRouteInputSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        header_route = header_route_update(
            header_route=header_route, data=serializer.validated_data
        )
        request_config_deploy(user_id=_user_id(request))
        return Response(HeaderRouteOutputSerializer(header_route).data)

    def delete(self, request, header_route_id):
        try:
            header_route = header_route_get(header_route_id=header_route_id)
        except HeaderRoute.DoesNotExist:
            return Response({"error": "Header route not found"}, status=status.HTTP_404_NOT_FOUND)

        header_route_delete(header_route=header_route)
        request_config_deploy(user_id=_user_id(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeployLogListApi(APIView):
    """
    GET /api/v1/routing/deploys/  — deploy history, newest first
    """

    def get(self, request):
        limit = _int_param(request, "limit") or 20
        logs = deploy_log_list(limit=min(limit, 200))
        return Response(ConfigDeployLogOutputSerializer(logs, many=True).data)


class DeployLogDetailApi(APIView):
    """
    GET /api/v1/routing/deploys/<id>/  — includes the rendered config
    """

    def get(self, request, log_id):
        try:
            log = deploy_log_get(log_id=log_id)
        except ConfigDeployLog.DoesNotExist:
            return Response({"error": "Deploy not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(ConfigDeployLogDetailSerializer(log).data)


class RoutingStatsApi(APIView):
    """
    GET /api/v1/routing/stats/  — rule counts for the dashboard
    """

    def get(self, request):
        return Response(routing_stats())


def _user_id(request) -> int | None:
    user = getattr(request, "user", None)
    return user.pk if user is not None and user.is_authenticated else None
