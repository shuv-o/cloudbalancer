from django.urls import path

from apps.routing.apis import (
    DeployLogDetailApi,
    DeployLogListApi,
    HeaderRouteDetailApi,
    HeaderRouteListCreateApi,
    RoutingRuleDetailApi,
    RoutingRuleListCreateApi,
    RoutingStatsApi,
)

urlpatterns = [
    path("rules/", RoutingRuleListCreateApi.as_view(), name="rule-list-create"),
    path("rules/<int:rule_id>/", RoutingRuleDetailApi.as_view(), name="rule-detail"),
    path(
        "rules/<int:rule_id>/header-routes/",
        HeaderRouteListCreateApi.as_view(),
        name="header-route-list-create",
    ),
    path(
        "header-routes/<int:header_route_id>/",
        HeaderRouteDetailApi.as_view(),
        name="header-route-detail",
    ),
    path("stats/", RoutingStatsApi.as_view(), name="routing-stats"),
    path("deploys/", DeployLogListApi.as_view(), name="deploy-log-list"),
    path("deploys/<int:log_id>/", DeployLogDetailApi.as_view(), name="deploy-log-detail"),
]
