from django.urls import path
from apps.domains.apis import DomainListCreateApi, DomainDetailApi, DomainStatsApi

urlpatterns = [
    path("", DomainListCreateApi.as_view(), name="domain-list-create"),
    path("stats/", DomainStatsApi.as_view(), name="domain-stats"),
    path("<int:domain_id>/", DomainDetailApi.as_view(), name="domain-detail"),
]
