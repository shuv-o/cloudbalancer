"""
DNS provider API views.

Admin-only. A Cloudflare API token can edit every record in an account and a
TSIG key can write to a zone, so the list of them, and certainly their
contents, are not operator business.

No endpoint ever returns a credential. The panel shows enough to tell two
providers apart and nothing that could be used -- a value that can be read
back out of an admin page is a value that leaves in a screenshot.
"""
import logging

from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.certificates.dns_providers import (
    TSIG_ALGORITHMS,
    DnsProvider,
    DnsProviderError,
    provider_for,
)
from apps.security.roles import IsAdmin

logger = logging.getLogger(__name__)


class DnsProviderOutputSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    credentials = serializers.CharField(source="describe_credentials", read_only=True)
    zones = serializers.ListField(source="normalised_zones", read_only=True)

    class Meta:
        model = DnsProvider
        fields = [
            "id", "name", "kind", "kind_label", "zones", "credentials",
            "server", "port", "tsig_name", "tsig_algorithm",
            "propagation_seconds", "is_enabled", "last_used_at", "last_error",
            "created_at",
        ]


class DnsProviderInputSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    kind = serializers.ChoiceField(choices=DnsProvider.Kind.choices)
    zones = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    is_enabled = serializers.BooleanField(default=True)
    propagation_seconds = serializers.IntegerField(min_value=0, max_value=1800, default=60)

    # Cloudflare
    api_token = serializers.CharField(required=False, allow_blank=True, default="")

    # RFC2136
    server = serializers.CharField(required=False, allow_blank=True, default="")
    port = serializers.IntegerField(required=False, min_value=1, max_value=65535, default=53)
    tsig_name = serializers.CharField(required=False, allow_blank=True, default="")
    tsig_algorithm = serializers.ChoiceField(
        choices=[(a, a) for a in TSIG_ALGORITHMS], required=False, default="HMAC-SHA256"
    )
    tsig_secret = serializers.CharField(required=False, allow_blank=True, default="")


class DnsProviderApi(APIView):
    """
    GET  /api/v1/certificates/dns-providers/  — who holds which zone
    POST /api/v1/certificates/dns-providers/  — add one
    """
    permission_classes = [IsAdmin]

    def get(self, request):
        providers = DnsProvider.objects.all()
        return Response({
            "providers": DnsProviderOutputSerializer(providers, many=True).data,
            "algorithms": list(TSIG_ALGORITHMS),
            # Which domains could have a wildcard issued, so the page can say
            # so rather than leaving someone to work it out.
            "coverage": _coverage(),
        })

    def post(self, request):
        serializer = DnsProviderInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        provider = DnsProvider(**serializer.validated_data)
        try:
            provider.validate_config()
        except DnsProviderError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        provider.save()
        logger.info("%s added DNS provider '%s'", request.user, provider.name)
        return Response(
            DnsProviderOutputSerializer(provider).data, status=status.HTTP_201_CREATED
        )


class DnsProviderDetailApi(APIView):
    """
    PATCH  /api/v1/certificates/dns-providers/<id>/  — change one
    DELETE /api/v1/certificates/dns-providers/<id>/  — remove one
    """
    permission_classes = [IsAdmin]

    def patch(self, request, provider_id: int):
        provider = DnsProvider.objects.filter(pk=provider_id).first()
        if provider is None:
            return Response({"error": "No such provider."}, status=status.HTTP_404_NOT_FOUND)

        serializer = DnsProviderInputSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        for field, value in serializer.validated_data.items():
            # A blank secret means "leave it alone", not "erase it". The panel
            # cannot show the current value, so it cannot send it back, and
            # without this every edit of a propagation timeout would wipe the
            # token.
            if field in ("api_token", "tsig_secret") and value == "":
                continue
            setattr(provider, field, value)

        try:
            provider.validate_config()
        except DnsProviderError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        provider.last_error = ""
        provider.save()
        return Response(DnsProviderOutputSerializer(provider).data)

    def delete(self, request, provider_id: int):
        provider = DnsProvider.objects.filter(pk=provider_id).first()
        if provider is None:
            return Response({"error": "No such provider."}, status=status.HTTP_404_NOT_FOUND)

        stranded = [
            name for name in _wildcard_domains()
            if provider_for(name) is not None
            and provider_for(name).pk == provider.pk
        ]
        if stranded:
            return Response(
                {"error": (
                    f"{', '.join(stranded)} can only be renewed through this "
                    "provider. Remove those domains first, or add another "
                    "provider covering their zones."
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        name = provider.name
        provider.delete()
        logger.info("%s removed DNS provider '%s'", request.user, name)
        return Response(status=status.HTTP_204_NO_CONTENT)


def _wildcard_domains() -> list[str]:
    from apps.domains.models import Domain

    return [
        d.name for d in Domain.objects.filter(name__startswith="*.")
    ]


def _coverage() -> list[dict]:
    """Every domain, and which provider could prove it over DNS-01."""
    from apps.domains.models import Domain

    rows = []
    for domain in Domain.objects.order_by("name"):
        provider = provider_for(domain.name)
        rows.append({
            "domain": domain.name,
            "is_wildcard": domain.name.startswith("*."),
            "provider": provider.name if provider else None,
            "zone": provider.covers(domain.name) if provider else None,
        })
    return rows
