"""
Scheduled backups.

The nightly job takes a backup, verifies it, and prunes. Verification runs in
the same task rather than on its own schedule so that a backup and the proof
it works never drift apart -- a verification pass that lags by a day reports
on yesterday's archive and says nothing about today's.
"""
import logging

from celery import shared_task

from apps.backups.models import BackupRun
from apps.backups.services import backup_create, backup_prune, backup_verify

logger = logging.getLogger(__name__)


@shared_task(name="backups.run_scheduled")
def run_scheduled_backup() -> dict:
    run = backup_create(trigger=BackupRun.Trigger.SCHEDULE)

    if run.status == BackupRun.Status.COMPLETE:
        backup_verify(run=run)

    pruned = backup_prune()

    return {
        "id": run.pk,
        "status": run.status,
        "verification": run.verification,
        "error": run.error,
        **pruned,
    }


@shared_task(name="backups.verify")
def verify_backup(run_id: int) -> dict:
    run = BackupRun.objects.filter(pk=run_id).first()
    if run is None:
        return {"error": "no such backup"}
    backup_verify(run=run)
    return {"id": run.pk, "verification": run.verification}


@shared_task(name="backups.create")
def create_backup(trigger: str = BackupRun.Trigger.MANUAL, user_id: int | None = None) -> dict:
    from django.contrib.auth.models import User

    actor = User.objects.filter(pk=user_id).first() if user_id else None
    run = backup_create(trigger=trigger, actor=actor)
    if run.status == BackupRun.Status.COMPLETE:
        backup_verify(run=run)
    return {"id": run.pk, "status": run.status, "verification": run.verification}
