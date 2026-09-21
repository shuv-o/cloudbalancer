"""Certificate API views."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.certificates.models import AcmeAccount, Certificate
from apps.certificates.selectors import (
    acme_account_get,
    acme_account_list,
    certificate_expiry_summary,
    certificate_get,
    certificate_list,
)
from apps.certificates.serializers import (
    AcmeAccountInputSerializer,
    AcmeAccountOutputSerializer,
    CertificateImportSerializer,
    CertificateOutputSerializer,
    CertificateRequestSerializer,
    CertificateSelfSignedSerializer,
    CertificateUpdateSerializer,
)
from apps.certificates.services import (
    CertificateError,
    acme_account_create,
    acme_account_delete,
    acme_account_update,
    certificate_delete,
    certificate_import,
    certificate_update,
)
from apps.certificates.tasks import (
    async_certificate_issue,
    async_certificate_renew,
    async_certificate_self_signed,
)
from apps.domains.models import Domain


class CertificateListApi(APIView):
    """
    GET /api/v1/certificates/  — every certificate the gateway knows about
    """

    def get(self, request):
        certificates = certificate_list(status=request.query_params.get("status"))
        return Response(CertificateOutputSerializer(certificates, many=True).data)


class CertificateDetailApi(APIView):
    """
    GET    /api/v1/certificates/<id>/  — one certificate
    PATCH  /api/v1/certificates/<id>/  — change renewal policy
    DELETE /api/v1/certificates/<id>/  — drop it and revert the domain to HTTP
    """

    def get(self, request, certificate_id):
        try:
            certificate = certificate_get(certificate_id=certificate_id)
        except Certificate.DoesNotExist:
            return Response({"error": "Certificate not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(CertificateOutputSerializer(certificate).data)

    def patch(self, request, certificate_id):
        try:
            certificate = certificate_get(certificate_id=certificate_id)
        except Certificate.DoesNotExist:
            return Response({"error": "Certificate not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = CertificateUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        certificate = certificate_update(certificate=certificate, data=serializer.validated_data)
        return Response(CertificateOutputSerializer(certificate).data)

    def delete(self, request, certificate_id):
        try:
            certificate = certificate_get(certificate_id=certificate_id)
        except Certificate.DoesNotExist:
            return Response({"error": "Certificate not found"}, status=status.HTTP_404_NOT_FOUND)

        certificate_delete(certificate=certificate)

        from apps.gateway.tasks import request_config_deploy
        request_config_deploy()

        return Response(status=status.HTTP_204_NO_CONTENT)


class CertificateRequestApi(APIView):
    """
    POST /api/v1/certificates/request/  — get a certificate from the CA

    Queued: talking to an ACME server takes long enough that holding a request
    open for it would be its own kind of outage.
    """

    def post(self, request):
        serializer = CertificateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if not Domain.objects.filter(pk=data["domain_id"]).exists():
            return Response({"error": "Domain not found"}, status=status.HTTP_404_NOT_FOUND)

        async_certificate_issue.delay(
            data["domain_id"],
            data.get("acme_account_id"),
            data.get("extra_domains") or [],
            data.get("force", False),
        )
        return Response(
            {"message": "Requesting a certificate. This usually takes under a minute."},
            status=status.HTTP_202_ACCEPTED,
        )


class CertificateSelfSignedApi(APIView):
    """
    POST /api/v1/certificates/self-signed/  — generate a local certificate
    """

    def post(self, request):
        serializer = CertificateSelfSignedSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if not Domain.objects.filter(pk=data["domain_id"]).exists():
            return Response({"error": "Domain not found"}, status=status.HTTP_404_NOT_FOUND)

        async_certificate_self_signed.delay(data["domain_id"], data["valid_days"])
        return Response(
            {"message": "Generating a self-signed certificate."},
            status=status.HTTP_202_ACCEPTED,
        )


class CertificateImportApi(APIView):
    """
    POST /api/v1/certificates/import/  — store an operator-supplied PEM pair
    """

    def post(self, request):
        serializer = CertificateImportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            domain = Domain.objects.get(pk=data["domain_id"])
        except Domain.DoesNotExist:
            return Response({"error": "Domain not found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            certificate = certificate_import(
                domain=domain,
                cert_pem=data["cert_pem"],
                key_pem=data["key_pem"],
            )
        except CertificateError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        from apps.gateway.tasks import request_config_deploy
        request_config_deploy()

        return Response(
            CertificateOutputSerializer(certificate).data,
            status=status.HTTP_201_CREATED,
        )


class CertificateRenewApi(APIView):
    """
    POST /api/v1/certificates/<id>/renew/  — renew now, ahead of schedule
    """

    def post(self, request, certificate_id):
        try:
            certificate = certificate_get(certificate_id=certificate_id)
        except Certificate.DoesNotExist:
            return Response({"error": "Certificate not found"}, status=status.HTTP_404_NOT_FOUND)

        if certificate.issuer != Certificate.Issuer.ACME:
            return Response(
                {
                    "error": (
                        f"{certificate.domain.name} uses a "
                        f"{certificate.get_issuer_display().lower()} certificate, "
                        "which this gateway cannot renew on its own."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        async_certificate_renew.delay(certificate_id, bool(request.data.get("force")))
        return Response(
            {"message": f"Renewing the certificate for {certificate.domain.name}."},
            status=status.HTTP_202_ACCEPTED,
        )


class CertificateSummaryApi(APIView):
    """
    GET /api/v1/certificates/summary/  — expiry counts for the dashboard
    """

    def get(self, request):
        return Response(certificate_expiry_summary())


# ---------------------------------------------------------------------------
# ACME accounts
# ---------------------------------------------------------------------------

class AcmeAccountListCreateApi(APIView):
    """
    GET  /api/v1/certificates/acme-accounts/
    POST /api/v1/certificates/acme-accounts/
    """

    def get(self, request):
        return Response(AcmeAccountOutputSerializer(acme_account_list(), many=True).data)

    def post(self, request):
        serializer = AcmeAccountInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            account = acme_account_create(**serializer.validated_data)
        except CertificateError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            AcmeAccountOutputSerializer(account).data,
            status=status.HTTP_201_CREATED,
        )


class AcmeAccountDetailApi(APIView):
    """
    PATCH  /api/v1/certificates/acme-accounts/<id>/
    DELETE /api/v1/certificates/acme-accounts/<id>/
    """

    def patch(self, request, account_id):
        try:
            account = acme_account_get(account_id=account_id)
        except AcmeAccount.DoesNotExist:
            return Response({"error": "ACME account not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = AcmeAccountInputSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        account = acme_account_update(account=account, data=serializer.validated_data)
        return Response(AcmeAccountOutputSerializer(account).data)

    def delete(self, request, account_id):
        try:
            account = acme_account_get(account_id=account_id)
        except AcmeAccount.DoesNotExist:
            return Response({"error": "ACME account not found"}, status=status.HTTP_404_NOT_FOUND)
        acme_account_delete(account=account)
        return Response(status=status.HTTP_204_NO_CONTENT)
