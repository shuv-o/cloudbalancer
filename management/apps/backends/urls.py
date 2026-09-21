from django.urls import path
from apps.backends.apis import (
    BackendListCreateApi,
    BackendDetailApi,
    BackendHealthCheckApi,
    BackendHealthSummaryApi,
    InstanceListCreateApi,
    InstanceDetailApi,
)

urlpatterns = [
    path("", BackendListCreateApi.as_view(), name="backend-list-create"),
    path("health/", BackendHealthSummaryApi.as_view(), name="backend-health-summary"),
    path("<int:backend_id>/", BackendDetailApi.as_view(), name="backend-detail"),
    path("<int:backend_id>/health-check/", BackendHealthCheckApi.as_view(), name="backend-health-check"),
    path("<int:backend_id>/instances/", InstanceListCreateApi.as_view(), name="instance-list-create"),
    path("<int:backend_id>/instances/<int:instance_id>/", InstanceDetailApi.as_view(), name="instance-detail"),
]
