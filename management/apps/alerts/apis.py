"""
Alert API views.

Reading alerts is an operator's business -- knowing a backend is down is part
of running traffic. Configuring where alerts go is an admin's, because a
channel is an outbound connection carrying what this gateway serves, and a
webhook URL is a credential.
"""
import hmac
import logging

from django.conf import settings
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.alerts.models import Alert, NotificationAttempt, NotificationChannel, Severity
from apps.alerts.serializers import (
    AlertOutputSerializer,
    AttemptOutputSerializer,
    ChannelInputSerializer,
    ChannelOutputSerializer,
)
from apps.alerts.services import (
    AlertError,
    alert_acknowledge,
    alert_raise,
    alert_resolve,
    alert_summary,
    channel_test,
    fingerprint,
)
from apps.security.roles import IsAdmin, Role

logger = logging.getLogger(__name__)


class AlertApi(APIView):
    """
    GET /api/v1/alerts/  — what is wrong, and what was
    """
    required_role = Role.VIEWER

    def get(self, request):
        alerts = Alert.objects.select_related("acknowledged_by")

        state = request.query_params.get("status")
        if state in (Alert.Status.FIRING, Alert.Status.RESOLVED):
            alerts = alerts.filter(status=state)

        severity = request.query_params.get("severity")
        if severity in Severity.values:
            alerts = alerts.filter(severity=severity)

        return Response({
            "summary": alert_summary(),
            "alerts": AlertOutputSerializer(alerts[:200], many=True).data,
        })


class AlertDetailApi(APIView):
    """
    POST /api/v1/alerts/<id>/acknowledge/  — say somebody is dealing with it
    """
    required_role = Role.OPERATOR

    def post(self, request, alert_id: int):
        alert = Alert.objects.filter(pk=alert_id).first()
        if alert is None:
            return Response({"error": "No such alert."}, status=status.HTTP_404_NOT_FOUND)

        alert_acknowledge(alert=alert, user=request.user)
        logger.info("%s acknowledged alert %s", request.user, alert.pk)
        return Response(AlertOutputSerializer(alert).data)


class ChannelApi(APIView):
    """
    GET  /api/v1/alerts/channels/  — where alerts go
    POST /api/v1/alerts/channels/  — add somewhere
    """
    permission_classes = [IsAdmin]

    def get(self, request):
        channels = NotificationChannel.objects.all()
        return Response({
            "channels": ChannelOutputSerializer(channels, many=True).data,
            "recent": AttemptOutputSerializer(
                NotificationAttempt.objects.select_related("alert", "channel")[:50],
                many=True,
            ).data,
            "email_configured": bool(settings.EMAIL_HOST),
        })

    def post(self, request):
        serializer = ChannelInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        channel = NotificationChannel.objects.create(**serializer.validated_data)
        logger.info("%s added alert channel '%s'", request.user, channel.name)
        return Response(
            ChannelOutputSerializer(channel).data, status=status.HTTP_201_CREATED
        )


class ChannelDetailApi(APIView):
    """
    PATCH  /api/v1/alerts/channels/<id>/  — change one
    DELETE /api/v1/alerts/channels/<id>/  — remove one
    """
    permission_classes = [IsAdmin]

    def patch(self, request, channel_id: int):
        channel = NotificationChannel.objects.filter(pk=channel_id).first()
        if channel is None:
            return Response({"error": "No such channel."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ChannelInputSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        for field, value in serializer.validated_data.items():
            setattr(channel, field, value)
        # Changing the configuration makes the old failure history irrelevant,
        # and leaving it would show a channel as broken after it was fixed.
        channel.consecutive_failures = 0
        channel.last_error = ""
        channel.save()

        return Response(ChannelOutputSerializer(channel).data)

    def delete(self, request, channel_id: int):
        channel = NotificationChannel.objects.filter(pk=channel_id).first()
        if channel is None:
            return Response({"error": "No such channel."}, status=status.HTTP_404_NOT_FOUND)

        name = channel.name
        channel.delete()
        logger.info("%s removed alert channel '%s'", request.user, name)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChannelTestApi(APIView):
    """
    POST /api/v1/alerts/channels/<id>/test/  — send a real message through it
    """
    permission_classes = [IsAdmin]

    def post(self, request, channel_id: int):
        channel = NotificationChannel.objects.filter(pk=channel_id).first()
        if channel is None:
            return Response({"error": "No such channel."}, status=status.HTTP_404_NOT_FOUND)

        try:
            detail = channel_test(channel=channel)
        except Exception as exc:
            # Deliberately not a 500. A channel that cannot deliver is a
            # finding, not a server fault, and the operator needs the reason.
            channel.consecutive_failures += 1
            channel.last_error = str(exc)[:1000]
            channel.save(update_fields=["consecutive_failures", "last_error", "updated_at"])
            return Response(
                {"ok": False, "error": str(exc)[:1000]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        channel.consecutive_failures = 0
        channel.last_error = ""
        channel.last_success_at = timezone.now()
        channel.save(update_fields=[
            "consecutive_failures", "last_error", "last_success_at", "updated_at"
        ])
        return Response({"ok": True, "detail": detail})


class AlertmanagerWebhookApi(APIView):
    """
    POST /api/v1/alerts/prometheus/  — where Alertmanager delivers

    Unauthenticated in the session sense, because Alertmanager has no session.
    It carries a shared secret instead, compared in constant time, and the
    endpoint is reachable only from inside the Compose network -- Nginx never
    routes to it.

    Prometheus alerts arrive here rather than going straight to Slack so that
    there is one history to read during an incident and one place to configure
    who hears about it. Two notification paths means two sets of channels to
    keep in step, and the one nobody updated is the one that matters.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        secret = settings.ALERTMANAGER_WEBHOOK_SECRET
        if secret:
            presented = request.headers.get("Authorization", "")
            expected = f"Bearer {secret}"
            if not hmac.compare_digest(presented, expected):
                logger.warning("Rejected an Alertmanager delivery with a bad secret")
                return Response(
                    {"error": "unauthorised"}, status=status.HTTP_401_UNAUTHORIZED
                )

        payload = request.data
        if not isinstance(payload, dict):
            return Response({"error": "expected an object"},
                            status=status.HTTP_400_BAD_REQUEST)

        handled = 0
        for item in payload.get("alerts", []) or []:
            labels = item.get("labels", {}) or {}
            annotations = item.get("annotations", {}) or {}

            name = labels.get("alertname") or "Prometheus alert"
            # Alertmanager's own fingerprint when it sends one; otherwise the
            # label set, which is what it computes its own from.
            key = item.get("fingerprint") or fingerprint(
                "prometheus", *sorted(f"{k}={v}" for k, v in labels.items())
            )

            if item.get("status") == "resolved":
                alert_resolve(key=key)
            else:
                severity = labels.get("severity", Severity.WARNING)
                if severity not in Severity.values:
                    severity = Severity.WARNING

                alert_raise(
                    key=key,
                    source=Alert.Source.PROMETHEUS,
                    severity=severity,
                    title=annotations.get("summary") or name,
                    body=annotations.get("description", ""),
                    runbook=annotations.get("runbook", ""),
                    labels=labels,
                )
            handled += 1

        return Response({"handled": handled})
