"""
URL configuration for the Proxy Balancer management API.

Everything the control panel uses lives under /api/v1/. The Prometheus
endpoint sits outside it because scrapers expect /metrics at the root.
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),

    # Scraped directly by Prometheus, not routed through the gateway.
    path("", include("django_prometheus.urls")),

    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/security/", include("apps.security.urls")),
    path("api/v1/domains/", include("apps.domains.urls")),
    path("api/v1/backends/", include("apps.backends.urls")),
    path("api/v1/routing/", include("apps.routing.urls")),
    path("api/v1/certificates/", include("apps.certificates.urls")),
    path("api/v1/monitoring/", include("apps.monitoring.urls")),
    path("api/v1/gateway/", include("apps.gateway.urls")),
]
