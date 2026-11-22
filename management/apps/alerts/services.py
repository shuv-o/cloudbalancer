"""
Raising alerts, and getting them to people.

Two rules shape the whole of this.

An alert that fires repeatedly is one alert. A backend flapping every thirty
seconds is a single problem, and a history recording it three hundred times is
a history nobody reads -- so occurrences are counted on one row rather than
appended as new ones.

Delivery never fails the thing that raised the alert. Notification runs
through Celery, and a channel that is down, slow, or misconfigured must not
turn "the certificate is expiring" into "the renewal task crashed". The
failure is recorded against the channel instead, where somebody can see that
the thing they are relying on has not worked for a week.
"""
import hashlib
import json
import logging
import re
import time

import httpx
from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.utils import timezone

from apps.alerts.models import (
    SEVERITY_RANK,
    Alert,
    NotificationAttempt,
    NotificationChannel,
    Severity,
)

logger = logging.getLogger(__name__)


class AlertError(Exception):
    """A channel could not be configured or reached."""


def fingerprint(*parts: str) -> str:
    """A stable id for 'the same problem', from whatever identifies it."""
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:48]


# ---------------------------------------------------------------------------
# Raising and resolving
# ---------------------------------------------------------------------------

def alert_raise(
    *,
    key: str,
    title: str,
    severity: str = Severity.WARNING,
    body: str = "",
    runbook: str = "",
    labels: dict | None = None,
    source: str = Alert.Source.CONTROL_PLANE,
) -> Alert:
    """
    Record that something is wrong, and notify if it is worth notifying about.

    Raising the same key again updates the existing alert rather than making
    another. An alert that had resolved and recurs starts a fresh episode --
    same row, new started_at -- because "it came back" is a different fact
    from "it never went away", and the duration shown should mean something.
    """
    labels = labels or {}
    now = timezone.now()

    alert, created = Alert.objects.get_or_create(
        fingerprint=key,
        defaults={
            "source": source,
            "severity": severity,
            "title": title,
            "body": body,
            "runbook": runbook,
            "labels": labels,
            "started_at": now,
            "last_seen_at": now,
        },
    )

    if not created:
        recurred = alert.status == Alert.Status.RESOLVED
        if recurred:
            alert.started_at = now
            alert.occurrences = 1
            # A new episode is a new thing to be told about, and a new thing
            # to acknowledge. Carrying the old acknowledgement forward would
            # silence a problem that has come back.
            alert.acknowledged_at = None
            alert.acknowledged_by = None
            alert.last_notified_at = None
        else:
            alert.occurrences += 1

        alert.status = Alert.Status.FIRING
        alert.severity = severity
        alert.title = title
        alert.body = body
        alert.runbook = runbook
        alert.labels = labels
        alert.last_seen_at = now
        alert.resolved_at = None
        alert.save()

    _queue_notification(alert, resolution=False)
    return alert


def alert_resolve(*, key: str) -> Alert | None:
    """
    Mark a problem as over, and say so if anyone was told it had started.

    Resolving something nobody was notified about sends nothing. A message
    saying an alert cleared, to someone who never heard it fire, is noise
    that teaches people to ignore the channel.
    """
    alert = Alert.objects.filter(fingerprint=key, status=Alert.Status.FIRING).first()
    if alert is None:
        return None

    alert.status = Alert.Status.RESOLVED
    alert.resolved_at = timezone.now()
    alert.save(update_fields=["status", "resolved_at"])

    if alert.last_notified_at:
        _queue_notification(alert, resolution=True)

    return alert


def alert_acknowledge(*, alert: Alert, user) -> Alert:
    """
    Say somebody is dealing with it, which stops the reminders.

    Not the same as resolving. The problem is still there and still shown;
    what stops is the repetition, because the point of repeating was to find
    somebody and somebody has been found.
    """
    alert.acknowledged_at = timezone.now()
    alert.acknowledged_by = user if getattr(user, "pk", None) else None
    alert.save(update_fields=["acknowledged_at", "acknowledged_by"])
    return alert


def _queue_notification(alert: Alert, *, resolution: bool) -> None:
    """Hand off to Celery, swallowing a broker that is not there."""
    from apps.alerts.tasks import deliver_alert

    try:
        deliver_alert.delay(alert.pk, resolution=resolution)
    except Exception:
        # A broker that is unreachable must not propagate into the caller.
        # Whatever raised this alert was reporting a problem, and turning that
        # report into a second exception loses the first one.
        logger.exception("Could not queue a notification for alert %s", alert.pk)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _should_notify(alert: Alert, *, resolution: bool) -> bool:
    if resolution:
        return True
    if alert.is_acknowledged:
        return False
    if alert.last_notified_at is None:
        return True

    quiet = settings.ALERT_REPEAT_MINUTES * 60
    return (timezone.now() - alert.last_notified_at).total_seconds() >= quiet


def deliver(alert: Alert, *, resolution: bool = False) -> list[NotificationAttempt]:
    """Send one alert to every channel that wants it."""
    if not _should_notify(alert, resolution=resolution):
        return []

    channels = [
        channel
        for channel in NotificationChannel.objects.filter(is_enabled=True)
        if channel.wants(alert.severity)
    ]
    if not channels:
        return []

    attempts = []
    for channel in channels:
        try:
            detail = _send(channel, alert, resolution=resolution)
            succeeded = True
        except Exception as exc:
            detail = str(exc)[:1000]
            succeeded = False
            logger.error("Alert %s to %s failed: %s", alert.pk, channel.name, exc)

        attempts.append(
            NotificationAttempt.objects.create(
                alert=alert, channel=channel,
                succeeded=succeeded, detail=detail, is_resolution=resolution,
            )
        )

        if succeeded:
            channel.consecutive_failures = 0
            channel.last_error = ""
            channel.last_success_at = timezone.now()
        else:
            channel.consecutive_failures += 1
            channel.last_error = detail
        channel.save(update_fields=[
            "consecutive_failures", "last_error", "last_success_at", "updated_at"
        ])

    if not resolution and any(a.succeeded for a in attempts):
        alert.last_notified_at = timezone.now()
        alert.save(update_fields=["last_notified_at"])

    return attempts


def _subject(alert: Alert, *, resolution: bool) -> str:
    if resolution:
        return f"[RESOLVED] {alert.title}"
    return f"[{alert.get_severity_display().upper()}] {alert.title}"


def _plain_body(alert: Alert, *, resolution: bool) -> str:
    lines = [alert.title, ""]
    if alert.body:
        lines += [alert.body, ""]

    lines.append(f"Severity:  {alert.get_severity_display()}")
    lines.append(f"Source:    {alert.get_source_display()}")
    lines.append(f"Started:   {alert.started_at:%Y-%m-%d %H:%M:%S %Z}")
    if resolution and alert.resolved_at:
        lines.append(f"Resolved:  {alert.resolved_at:%Y-%m-%d %H:%M:%S %Z}")
        lines.append(f"Lasted:    {int(alert.duration_seconds // 60)} minutes")
    elif alert.occurrences > 1:
        lines.append(f"Seen:      {alert.occurrences} times")

    if alert.labels:
        lines.append("")
        for name, value in sorted(alert.labels.items()):
            lines.append(f"  {name}: {value}")

    if alert.runbook and not resolution:
        lines += ["", "What to do:", alert.runbook]

    if settings.ALERT_PANEL_URL:
        lines += ["", f"Panel: {settings.ALERT_PANEL_URL.rstrip('/')}/alerts"]

    return "\n".join(lines)


def _send(channel: NotificationChannel, alert: Alert, *, resolution: bool) -> str:
    if channel.kind == NotificationChannel.Kind.EMAIL:
        return _send_email(channel, alert, resolution=resolution)
    if channel.kind == NotificationChannel.Kind.SLACK:
        return _send_slack(channel, alert, resolution=resolution)
    if channel.kind == NotificationChannel.Kind.WEBHOOK:
        return _send_webhook(channel, alert, resolution=resolution)
    if channel.kind == NotificationChannel.Kind.DISCORD:
        return _send_discord(channel, alert, resolution=resolution)
    raise AlertError(f"'{channel.kind}' is not a kind of channel this can send to.")


def _send_email(channel: NotificationChannel, alert: Alert, *, resolution: bool) -> str:
    recipients = channel.config.get("to") or []
    if not recipients:
        raise AlertError("This email channel has no recipients.")
    if not settings.EMAIL_HOST:
        raise AlertError(
            "No mail server is configured. Set EMAIL_HOST and the rest of the "
            "EMAIL_ settings, or use a webhook instead."
        )

    connection = get_connection(fail_silently=False)
    message = EmailMessage(
        subject=_subject(alert, resolution=resolution),
        body=_plain_body(alert, resolution=resolution),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=recipients,
        connection=connection,
    )
    message.send(fail_silently=False)
    return f"sent to {', '.join(recipients)}"


_SLACK_COLOUR = {
    Severity.INFO: "#4a90d9",
    Severity.WARNING: "#d9a441",
    Severity.CRITICAL: "#d14b4b",
}


def _send_slack(channel: NotificationChannel, alert: Alert, *, resolution: bool) -> str:
    url = channel.config.get("url")
    if not url:
        raise AlertError("This Slack channel has no webhook URL.")

    fields = [
        {"title": "Severity", "value": alert.get_severity_display(), "short": True},
        {"title": "Source", "value": alert.get_source_display(), "short": True},
    ]
    for name, value in sorted(alert.labels.items())[:6]:
        fields.append({"title": name, "value": str(value), "short": True})

    payload = {
        "text": _subject(alert, resolution=resolution),
        "attachments": [{
            "color": "#4aa564" if resolution else _SLACK_COLOUR[Severity(alert.severity)],
            "title": alert.title,
            "text": (alert.body or "")[:2000],
            "fields": fields,
            "footer": "CloudBalancer",
            "ts": int(alert.started_at.timestamp()),
        }],
    }

    response = httpx.post(url, json=payload, timeout=settings.ALERT_HTTP_TIMEOUT)
    response.raise_for_status()
    return f"posted, {response.status_code}"


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

# A Discord webhook, and nothing else. Checked on save so a pasted channel
# link or a typo fails in the form rather than during the incident it was
# meant to report -- and so this channel can only ever post to Discord.
DISCORD_WEBHOOK = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?"
    r"/webhooks/\d{15,25}/[A-Za-z0-9_-]{30,100}/?$"
)

# Who a critical alert may ping. Anything else in the text is never a ping:
# alert bodies quote log lines and hostnames, and an "@everyone" inside one
# must not page the whole server.
DISCORD_MENTION = re.compile(r"^(?:@here|@everyone|<@&\d{15,25}>|<@\d{15,25}>)$")

_DISCORD_COLOUR = {
    Severity.INFO: 0x4A90D9,
    Severity.WARNING: 0xD9A441,
    Severity.CRITICAL: 0xD14B4B,
}
_DISCORD_RESOLVED = 0x4AA564

# Discord rejects the whole message if any limit is exceeded, so everything is
# cut to fit first: a truncated alert is far better than none.
_DISCORD_TITLE, _DISCORD_DESCRIPTION = 256, 3000
_DISCORD_FIELD_NAME, _DISCORD_FIELD_VALUE = 64, 1000
_DISCORD_LABEL_VALUE = 200
# Each part has its own limit, and the embed has one over all of them
# together: 6000 characters. Kept under it with room to spare.
_DISCORD_EMBED_TOTAL = 5800


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _discord_payload(channel: NotificationChannel, alert: Alert, *, resolution: bool) -> dict:
    started = int(alert.started_at.timestamp())
    fields = [
        {"name": "Severity", "value": alert.get_severity_display(), "inline": True},
        {"name": "Source", "value": alert.get_source_display(), "inline": True},
        # Discord renders <t:…:R> in each reader's own time zone, as "5 minutes ago".
        {"name": "Started", "value": f"<t:{started}:R>", "inline": True},
    ]
    if resolution and alert.resolved_at:
        fields.append({
            "name": "Lasted",
            "value": f"{int(alert.duration_seconds // 60)} minutes",
            "inline": True,
        })
    elif alert.occurrences > 1:
        fields.append({"name": "Seen", "value": f"{alert.occurrences} times", "inline": True})

    for name, value in sorted((alert.labels or {}).items())[:6]:
        fields.append({
            "name": _clip(name, _DISCORD_FIELD_NAME),
            "value": _clip(f"`{value}`", _DISCORD_LABEL_VALUE),
            "inline": True,
        })
    if alert.runbook and not resolution:
        fields.append({
            "name": "What to do",
            "value": _clip(alert.runbook, _DISCORD_FIELD_VALUE),
            "inline": False,
        })

    embed = {
        "title": _clip(_subject(alert, resolution=resolution), _DISCORD_TITLE),
        "description": _clip(alert.body, _DISCORD_DESCRIPTION),
        "color": _DISCORD_RESOLVED if resolution else _DISCORD_COLOUR[Severity(alert.severity)],
        "fields": fields,
        "footer": {"text": "CloudBalancer"},
        "timestamp": (alert.resolved_at if resolution and alert.resolved_at
                      else alert.started_at).isoformat(),
    }
    if settings.ALERT_PANEL_URL:
        embed["url"] = f"{settings.ALERT_PANEL_URL.rstrip('/')}/alerts"

    # Over the total, the description gives way first -- the title, severity
    # and labels are what someone glancing at the channel needs -- and the
    # runbook after it.
    def size() -> int:
        return (len(embed["title"]) + len(embed["description"])
                + len(embed["footer"]["text"])
                + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"]))

    excess = size() - _DISCORD_EMBED_TOTAL
    if excess > 0:
        embed["description"] = _clip(
            embed["description"], max(len(embed["description"]) - excess, 200)
        )
    excess = size() - _DISCORD_EMBED_TOTAL
    if excess > 0 and fields[-1]["name"] == "What to do":
        fields[-1]["value"] = _clip(fields[-1]["value"], max(len(fields[-1]["value"]) - excess, 100))

    # Ping only when it matters: a critical alert starting. A resolution, or a
    # warning, waking people up teaches them to mute the channel.
    mention = (channel.config.get("mention") or "").strip()
    ping = mention if (mention and not resolution
                       and alert.severity == Severity.CRITICAL) else ""

    allowed = {"parse": []}
    if ping in ("@here", "@everyone"):
        allowed = {"parse": ["everyone"]}
    elif ping.startswith("<@&"):
        allowed = {"parse": [], "roles": [ping[3:-1]]}
    elif ping.startswith("<@"):
        allowed = {"parse": [], "users": [ping[2:-1]]}

    return {
        "username": "CloudBalancer",
        "content": ping,
        "embeds": [embed],
        "allowed_mentions": allowed,
    }


def _send_discord(channel: NotificationChannel, alert: Alert, *, resolution: bool) -> str:
    url = (channel.config.get("url") or "").strip()
    if not DISCORD_WEBHOOK.match(url):
        raise AlertError("This Discord channel does not have a valid webhook URL.")

    payload = _discord_payload(channel, alert, resolution=resolution)

    # Errors are reported without the URL. httpx puts the request URL in its
    # own exception text, a Discord webhook URL carries the token that lets
    # anyone post as this channel, and the error is saved on the channel and
    # shown in the panel.
    for attempt in (1, 2):
        try:
            response = httpx.post(
                url, params={"wait": "true"}, json=payload,
                timeout=settings.ALERT_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise AlertError(f"Discord could not be reached: {type(exc).__name__}") from None

        if response.status_code == 429 and attempt == 1:
            # Rate limited. One short wait is worth it -- an alert storm is
            # exactly when this happens -- a long one is not: this runs in a
            # worker other alerts are queued behind.
            try:
                retry_after = float(response.json().get("retry_after", 0))
            except Exception:
                retry_after = 0
            if 0 < retry_after <= 5:
                time.sleep(retry_after)
                continue
        break

    if response.status_code in (401, 404):
        raise AlertError(
            f"Discord refused the webhook ({response.status_code}). It was probably "
            "deleted or regenerated in Discord; create a new one and update this channel."
        )
    if response.status_code == 429:
        raise AlertError("Discord is rate limiting this webhook; the alert was not posted.")
    if response.status_code >= 400:
        try:
            reason = _clip(response.json().get("message", ""), 200)
        except Exception:
            reason = ""
        raise AlertError(f"Discord rejected the message ({response.status_code}){': ' + reason if reason else ''}.")

    return f"posted to Discord, {response.status_code}"


def _send_webhook(channel: NotificationChannel, alert: Alert, *, resolution: bool) -> str:
    url = channel.config.get("url")
    if not url:
        raise AlertError("This webhook channel has no URL.")

    payload = {
        "status": "resolved" if resolution else "firing",
        "fingerprint": alert.fingerprint,
        "severity": alert.severity,
        "source": alert.source,
        "title": alert.title,
        "body": alert.body,
        "runbook": alert.runbook,
        "labels": alert.labels,
        "started_at": alert.started_at.isoformat(),
        "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
        "occurrences": alert.occurrences,
    }

    headers = {"Content-Type": "application/json"}
    # A shared secret the receiver can check, so an endpoint that accepts
    # these is not an endpoint anyone can post to.
    token = channel.config.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(channel.config.get("headers") or {})

    response = httpx.post(
        url, content=json.dumps(payload), headers=headers,
        timeout=settings.ALERT_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    return f"posted, {response.status_code}"


def channel_test(*, channel: NotificationChannel) -> str:
    """
    Send a real message through a channel, now.

    A channel is only configured correctly in the sense that somebody typed
    something into it, until something has actually been delivered. The usual
    way to discover otherwise is during an incident.
    """
    probe = Alert(
        fingerprint="test",
        source=Alert.Source.CONTROL_PLANE,
        severity=Severity.INFO,
        title="Test alert from CloudBalancer",
        body=(
            "Somebody pressed the test button on the alerts page. Nothing is "
            "wrong. If you can read this, this channel works."
        ),
        labels={"channel": channel.name},
        started_at=timezone.now(),
    )
    return _send(channel, probe, resolution=False)


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

def alert_summary() -> dict:
    """The header of the alerts page, and the badge in the rail."""
    firing = Alert.objects.filter(status=Alert.Status.FIRING)
    channels = NotificationChannel.objects.all()
    enabled = [c for c in channels if c.is_enabled]

    warnings = []
    if not enabled:
        warnings.append(
            "No channel is enabled, so nothing here reaches anybody. Alerts "
            "are recorded and shown on this page and nowhere else."
        )
    broken = [c for c in enabled if not c.is_healthy]
    if broken:
        warnings.append(
            "Delivery is failing for "
            + ", ".join(c.name for c in broken)
            + ". A channel somebody is relying on that has not worked for a "
            "week is worse than no channel at all."
        )

    return {
        "firing": firing.count(),
        "critical": firing.filter(severity=Severity.CRITICAL).count(),
        "unacknowledged": firing.filter(acknowledged_at__isnull=True).count(),
        "channels": len(enabled),
        "warnings": warnings,
    }
