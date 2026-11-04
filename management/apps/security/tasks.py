"""
Scheduled maintenance for the security tables.

Both the audit trail and the sign-in log are append-only and written on every
request that changes something. Left alone they grow until the disk notices,
and on a gateway that is a self-inflicted outage.
"""
import logging

from celery import shared_task
from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from apps.security.models import AccountLock, AuditEvent, LoginAttempt

logger = logging.getLogger(__name__)


@shared_task(name="security.prune_history")
def async_prune_history() -> dict:
    """
    Drop records past their retention window.

    The two windows differ on purpose. Sign-in attempts answer "is someone
    trying right now", which is a question about the last few weeks. The audit
    trail answers "who changed this, and when", which is a question people ask
    about things that happened months ago -- so it is kept far longer.

    Deleted in batches: a single DELETE over a year of rows takes a lock long
    enough to be noticed by everything else using the table.
    """
    now = timezone.now()
    removed = {}

    for model, days, label in (
        (LoginAttempt, settings.LOGIN_ATTEMPT_RETENTION_DAYS, "sign-in attempts"),
        (AuditEvent, settings.AUDIT_RETENTION_DAYS, "audit events"),
    ):
        cutoff = now - timedelta(days=days)
        total = 0
        while True:
            batch = list(
                model.objects.filter(created_at__lt=cutoff)
                .values_list("pk", flat=True)[:5000]
            )
            if not batch:
                break
            deleted, _ = model.objects.filter(pk__in=batch).delete()
            total += deleted
        removed[label] = total

    # Locks that expired long ago are not evidence of anything.
    expired_locks, _ = AccountLock.objects.filter(
        locked_until__lt=now - timedelta(days=7)
    ).delete()
    removed["expired locks"] = expired_locks

    if any(removed.values()):
        logger.info("Pruned history: %s", removed)
    return removed
