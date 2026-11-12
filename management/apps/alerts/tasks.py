"""
Alert delivery and the periodic checks.

Delivery is a task because a channel that is down, slow or misconfigured must
not fail whatever raised the alert. A certificate renewal reporting that it
could not reach Slack, instead of that the certificate is expiring, loses the
part that mattered.
"""
import logging

from celery import shared_task

from apps.alerts.checks import run_all_checks
from apps.alerts.models import Alert
from apps.alerts.services import deliver

logger = logging.getLogger(__name__)


@shared_task(name="alerts.deliver", bind=True, max_retries=3, default_retry_delay=60)
def deliver_alert(self, alert_id: int, resolution: bool = False) -> dict:
    alert = Alert.objects.filter(pk=alert_id).first()
    if alert is None:
        return {"error": "no such alert"}

    attempts = deliver(alert, resolution=resolution)
    return {
        "alert": alert_id,
        "sent": sum(1 for a in attempts if a.succeeded),
        "failed": sum(1 for a in attempts if not a.succeeded),
    }


@shared_task(name="alerts.run_checks")
def run_checks() -> dict:
    """Ask every control-plane check whether its condition holds."""
    return run_all_checks()


@shared_task(name="alerts.prune")
def prune_alerts() -> dict:
    """
    Trim resolved alerts and delivery history.

    Firing alerts are never pruned however old: an alert nobody has dealt
    with for three months is the most interesting row in the table, not the
    least.
    """
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    from apps.alerts.models import NotificationAttempt

    cutoff = timezone.now() - timedelta(days=settings.ALERT_RETENTION_DAYS)
    resolved, _ = Alert.objects.filter(
        status=Alert.Status.RESOLVED, resolved_at__lt=cutoff
    ).delete()
    attempts, _ = NotificationAttempt.objects.filter(created_at__lt=cutoff).delete()

    return {"resolved_removed": resolved, "attempts_removed": attempts}
