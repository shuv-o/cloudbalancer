"""
Domain API views.

Thin views following the HackSoft pattern: validate input with serializers,
delegate to services for writes, and selectors for reads.
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.domains.models import Domain
from apps.domains.selectors import domain_list_with_rule_counts, domain_get, domain_stats
from apps.domains.serializers import DomainInputSerializer, DomainOutputSerializer
from apps.domains.services import domain_create, domain_update, domain_delete
from apps.gateway.tasks import request_config_deploy


class DomainListCreateApi(APIView):
    """
    GET  /api/v1/domains/           — List all domains
    POST /api/v1/domains/           — Create a new domain
    """

    def get(self, request):
        domains = domain_list_with_rule_counts()
        serializer = DomainOutputSerializer(domains, many=True)
        return Response(serializer.data)

    def post(self, request):
        input_serializer = DomainInputSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)

        domain = domain_create(**input_serializer.validated_data)

        # Deploy straight away so the domain's ACME challenge path is live
        # before anyone asks for a certificate for it.
        request_config_deploy(user_id=_user_id(request))

        output_serializer = DomainOutputSerializer(domain)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED)


class DomainDetailApi(APIView):
    """
    GET    /api/v1/domains/<id>/    — Get domain details
    PUT    /api/v1/domains/<id>/    — Update a domain
    DELETE /api/v1/domains/<id>/    — Delete a domain
    """

    def get(self, request, domain_id):
        try:
            domain = domain_get(domain_id=domain_id)
        except Domain.DoesNotExist:
            return Response(
                {"error": "Domain not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = DomainOutputSerializer(domain)
        return Response(serializer.data)

    def patch(self, request, domain_id):
        try:
            domain = domain_get(domain_id=domain_id)
        except Domain.DoesNotExist:
            return Response(
                {"error": "Domain not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        input_serializer = DomainInputSerializer(data=request.data, partial=True)
        input_serializer.is_valid(raise_exception=True)

        domain = domain_update(domain=domain, data=input_serializer.validated_data)
        request_config_deploy(user_id=_user_id(request))

        output_serializer = DomainOutputSerializer(domain)
        return Response(output_serializer.data)

    # PUT kept so clients that send a whole object still work.
    put = patch

    def delete(self, request, domain_id):
        try:
            domain = domain_get(domain_id=domain_id)
        except Domain.DoesNotExist:
            return Response(
                {"error": "Domain not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        domain_delete(domain=domain)
        request_config_deploy(user_id=_user_id(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


class DomainStatsApi(APIView):
    """
    GET /api/v1/domains/stats/      — Domain statistics for dashboard
    """

    def get(self, request):
        stats = domain_stats()
        return Response(stats)


def _user_id(request) -> int | None:
    user = getattr(request, "user", None)
    return user.pk if user is not None and user.is_authenticated else None
