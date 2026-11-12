"""
Backup API views.

Admin-only throughout, and for a sharper reason than the rest of the security
app: an archive contains every TLS private key the gateway serves plus the
whole audit trail, so the list of them, their digests and their contents are
all admin business.
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.backups.models import BackupRun
from apps.backups.serializers import BackupRunOutputSerializer
from apps.backups.services import backup_health, backup_prune
from apps.security.roles import IsAdmin

logger = logging.getLogger(__name__)


class BackupApi(APIView):
    """
    GET  /api/v1/backups/  — every backup, newest first, with its health
    POST /api/v1/backups/  — take one now
    """
    permission_classes = [IsAdmin]

    def get(self, request):
        runs = BackupRun.objects.select_related("started_by")[:50]
        return Response({
            "health": backup_health(),
            "backups": BackupRunOutputSerializer(runs, many=True).data,
        })

    def post(self, request):
        from apps.backups.tasks import create_backup

        # Queued rather than run inline: a dump of a large database would hold
        # a worker thread open well past any sensible request timeout, and the
        # panel would show a failure for a backup that was in fact running.
        create_backup.delay(
            trigger=BackupRun.Trigger.MANUAL, user_id=request.user.pk
        )
        logger.info("%s started a backup", request.user)
        return Response(
            {"started": True,
             "note": "Running in the background. It will appear here when it finishes."},
            status=status.HTTP_202_ACCEPTED,
        )


class BackupDetailApi(APIView):
    """
    POST   /api/v1/backups/<id>/verify/  — restore it into a scratch database
    DELETE /api/v1/backups/<id>/         — remove the archive and its record
    """
    permission_classes = [IsAdmin]

    def post(self, request, backup_id: int):
        from apps.backups.tasks import verify_backup

        run = BackupRun.objects.filter(pk=backup_id).first()
        if run is None:
            return Response({"error": "No such backup."}, status=status.HTTP_404_NOT_FOUND)

        verify_backup.delay(run.pk)
        return Response({"started": True}, status=status.HTTP_202_ACCEPTED)

    def delete(self, request, backup_id: int):
        from apps.backups.services import backup_dir

        run = BackupRun.objects.filter(pk=backup_id).first()
        if run is None:
            return Response({"error": "No such backup."}, status=status.HTTP_404_NOT_FOUND)

        if run.is_restorable and not BackupRun.objects.filter(
            status=BackupRun.Status.COMPLETE,
            verification=BackupRun.Verification.PASSED,
        ).exclude(pk=run.pk).exists():
            return Response(
                {"error": "This is the only backup known to restore. Take "
                          "another one before removing it."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        path = backup_dir() / run.filename if run.filename else None
        if path and path.exists():
            path.unlink()
        run.delete()
        logger.info("%s deleted backup %s", request.user, run.filename)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BackupPruneApi(APIView):
    """
    POST /api/v1/backups/prune/  — apply the retention policy now
    """
    permission_classes = [IsAdmin]

    def post(self, request):
        return Response(backup_prune())
