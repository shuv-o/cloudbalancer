"""Security API views."""
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.security.selectors import (
    audit_list,
    blocked_address_list,
    protection_get,
    protection_summary,
    login_attempt_list,
    operator_list,
    policy_get,
    security_summary,
)
from apps.security.serializers import (
    AuditEventOutputSerializer,
    BlockedAddressInputSerializer,
    BlockedAddressOutputSerializer,
    ProtectionInputSerializer,
    ProtectionOutputSerializer,
    LoginAttemptOutputSerializer,
    PolicyInputSerializer,
    PolicyOutputSerializer,
    TotpConfirmSerializer,
    TotpDisableSerializer,
    TotpResetSerializer,
)
from apps.security.services import (
    SecurityError,
    address_block,
    address_unblock,
    policy_update,
    protection_update,
    totp_begin_enrollment,
    totp_confirm_enrollment,
    totp_disable,
    totp_reset_for_user,
)


class IsSuperuser(IsAdminUser):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_superuser)


class PolicyApi(APIView):
    """
    GET   /api/v1/security/policy/  — how the panel may be reached
    PATCH /api/v1/security/policy/  — change it, then redeploy the gateway
    """
    permission_classes = [IsSuperuser]

    def get(self, request):
        return Response(PolicyOutputSerializer(policy_get()).data)

    def patch(self, request):
        serializer = PolicyInputSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        try:
            policy = policy_update(policy=policy_get(), data=serializer.validated_data)
        except SecurityError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # The panel's own server block is rendered from this policy, so a change
        # here means nothing until the gateway picks it up.
        from apps.gateway.tasks import request_config_deploy

        request_config_deploy(user_id=request.user.pk)

        return Response(PolicyOutputSerializer(policy).data)


class SecuritySummaryApi(APIView):
    """
    GET /api/v1/security/summary/  — what is protecting the panel, and who is trying
    """
    permission_classes = [IsSuperuser]

    def get(self, request):
        return Response(security_summary())


class AuditApi(APIView):
    """
    GET /api/v1/security/audit/  — everything that changed, and who changed it
    """
    permission_classes = [IsSuperuser]

    def get(self, request):
        limit = min(int(request.query_params.get("limit", 100)), 500)
        return Response(AuditEventOutputSerializer(audit_list(limit=limit), many=True).data)


class LoginAttemptApi(APIView):
    """
    GET /api/v1/security/attempts/  — recent sign-in attempts
    """
    permission_classes = [IsSuperuser]

    def get(self, request):
        limit = min(int(request.query_params.get("limit", 100)), 500)
        failures_only = request.query_params.get("failures") == "1"
        attempts = login_attempt_list(limit=limit, failures_only=failures_only)
        return Response(LoginAttemptOutputSerializer(attempts, many=True).data)


class OperatorApi(APIView):
    """
    GET /api/v1/security/operators/  — accounts, and whether each has a second factor
    """
    permission_classes = [IsSuperuser]

    def get(self, request):
        return Response({"operators": operator_list()})


# ---------------------------------------------------------------------------
# Second factor
# ---------------------------------------------------------------------------

class TotpSetupApi(APIView):
    """
    POST /api/v1/security/totp/setup/  — begin binding an authenticator app

    Returns the secret and a QR code. Nothing changes about signing in until
    the code is confirmed, so an abandoned setup cannot lock anyone out.
    """

    def post(self, request):
        try:
            return Response(totp_begin_enrollment(user=request.user))
        except SecurityError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class TotpConfirmApi(APIView):
    """
    POST /api/v1/security/totp/confirm/  — prove the app works

    The recovery codes come back here and are never shown again: only their
    hashes are stored.
    """

    def post(self, request):
        serializer = TotpConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            codes = totp_confirm_enrollment(
                user=request.user, code=serializer.validated_data["code"]
            )
        except SecurityError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "recovery_codes": codes,
            "message": (
                "Save these somewhere safe. Each one signs you in once if you lose "
                "your authenticator, and they will not be shown again."
            ),
        })


class TotpDisableApi(APIView):
    """
    POST /api/v1/security/totp/disable/  — remove your own authenticator
    """

    def post(self, request):
        serializer = TotpDisableSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            totp_disable(
                user=request.user,
                password=serializer.validated_data["password"],
                code=serializer.validated_data["code"],
            )
        except SecurityError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": "Authenticator removed."})


class TotpResetApi(APIView):
    """
    POST /api/v1/security/totp/reset/  — clear another operator's authenticator

    The break-glass path for a lost device. Superusers only, and recorded.
    """
    permission_classes = [IsSuperuser]

    def post(self, request):
        serializer = TotpResetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        target = User.objects.filter(pk=serializer.validated_data["user_id"]).first()
        if target is None:
            return Response({"error": "No such operator."}, status=status.HTTP_404_NOT_FOUND)

        try:
            totp_reset_for_user(actor=request.user, target_user=target)
        except SecurityError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            "message": (
                f"{target.get_username()} will be asked to set up a new authenticator "
                "at their next sign-in."
            )
        })


class TotpStatusApi(APIView):
    """
    GET /api/v1/security/totp/status/  — whether this account has a second factor
    """

    def get(self, request):
        from apps.security.models import TotpDevice

        device = TotpDevice.objects.filter(user=request.user).first()
        return Response({
            "required": policy_get().require_totp,
            "configured": bool(device and device.is_confirmed),
            "recovery_codes_remaining": device.recovery_codes_remaining if device else 0,
            "last_used_at": device.last_used_at if device else None,
        })


# ---------------------------------------------------------------------------
# Traffic protection
# ---------------------------------------------------------------------------

class ProtectionApi(APIView):
    """
    GET   /api/v1/security/protection/  — per-address limits, and their limits
    PATCH /api/v1/security/protection/  — change them, then redeploy
    """
    permission_classes = [IsSuperuser]

    def get(self, request):
        return Response({
            **ProtectionOutputSerializer(protection_get()).data,
            **protection_summary(),
        })

    def patch(self, request):
        serializer = ProtectionInputSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        policy = protection_update(policy=protection_get(), data=serializer.validated_data)

        from apps.gateway.tasks import request_config_deploy
        request_config_deploy(user_id=request.user.pk)

        return Response({
            **ProtectionOutputSerializer(policy).data,
            **protection_summary(),
        })


class BlockedAddressApi(APIView):
    """
    GET  /api/v1/security/blocked/  — addresses refused at the gateway
    POST /api/v1/security/blocked/  — refuse one

    Shaped so fail2ban, CrowdSec or a script can write into it. Automatic
    banning is deliberately not built in here: those tools already do it
    properly, and a log-tailing loop would be a poor imitation.
    """
    permission_classes = [IsSuperuser]

    def get(self, request):
        return Response(
            BlockedAddressOutputSerializer(blocked_address_list(), many=True).data
        )

    def post(self, request):
        serializer = BlockedAddressInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            entry = address_block(
                cidr=data["cidr"],
                reason=data["reason"],
                note=data.get("note", ""),
                minutes=data.get("minutes"),
                actor=request.user,
            )
        except SecurityError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        from apps.gateway.tasks import request_config_deploy
        request_config_deploy(user_id=request.user.pk)

        return Response(
            BlockedAddressOutputSerializer(entry).data,
            status=status.HTTP_201_CREATED,
        )


class BlockedAddressDetailApi(APIView):
    """
    DELETE /api/v1/security/blocked/<id>/  — stop refusing an address
    """
    permission_classes = [IsSuperuser]

    def delete(self, request, blocked_id):
        from apps.security.models import BlockedAddress

        entry = BlockedAddress.objects.filter(pk=blocked_id).first()
        if entry is None:
            return Response({"error": "Not blocked."}, status=status.HTTP_404_NOT_FOUND)

        address_unblock(cidr=entry.cidr)

        from apps.gateway.tasks import request_config_deploy
        request_config_deploy(user_id=request.user.pk)

        return Response(status=status.HTTP_204_NO_CONTENT)
