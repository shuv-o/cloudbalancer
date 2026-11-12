"""
The conditions only the control plane can see.

Prometheus watches the gateway: request rates, error rates, whether Nginx is
answering. It cannot see that a certificate expires in six days, that last
night's backup failed verification, or that a deploy was rejected, because
none of those are metrics -- they are facts in a database.

Each check is written to be run repeatedly. They raise an alert while the
condition holds and resolve it when it stops, so running this every ten
minutes is how a problem that fixes itself stops being mentioned.
"""
import logging

from django.conf import settings
from django.utils import timezone

from apps.alerts.models import Alert, Severity
from apps.alerts.services import alert_raise, alert_resolve, fingerprint

logger = logging.getLogger(__name__)


def _settle(key: str, firing: bool, **raise_kwargs) -> None:
    """Raise while the condition holds, resolve the moment it stops."""
    if firing:
        alert_raise(key=key, **raise_kwargs)
    else:
        alert_resolve(key=key)


def check_certificates() -> int:
    """
    Certificates close to expiry, and renewals that have stopped working.

    Expiry is the failure with the longest warning and the worst consequence:
    every client sees it at once, at a moment nobody chose.
    """
    from apps.certificates.models import Certificate

    raised = 0
    now = timezone.now()

    for certificate in Certificate.objects.select_related("domain").exclude(
        not_after__isnull=True
    ):
        domain = certificate.domain.name if certificate.domain_id else "(no domain)"
        days = (certificate.not_after - now).days
        key = fingerprint("certificate-expiry", certificate.pk)

        if days <= settings.ALERT_CERT_CRITICAL_DAYS:
            severity = Severity.CRITICAL
        elif days <= settings.ALERT_CERT_WARNING_DAYS:
            severity = Severity.WARNING
        else:
            alert_resolve(key=key)
            continue

        expired = days < 0
        _settle(
            key,
            True,
            severity=severity,
            title=(
                f"Certificate for {domain} expired {abs(days)} days ago"
                if expired
                else f"Certificate for {domain} expires in {days} days"
            ),
            body=(
                f"Expires {certificate.not_after:%Y-%m-%d %H:%M %Z}. "
                + (
                    "Every client reaching this domain is seeing a TLS error."
                    if expired
                    else "Renewal runs nightly; this alert means it has not "
                    "succeeded yet."
                )
            ),
            runbook=(
                "Check the Certificates page for the last renewal error. A "
                "renewal that fails repeatedly is usually DNS no longer "
                "pointing here, or port 80 not reaching this gateway -- both "
                "of which break the HTTP-01 challenge."
            ),
            labels={"domain": domain, "certificate_id": certificate.pk},
        )
        raised += 1

    return raised


def check_backends() -> int:
    """A backend with nothing healthy behind it is an outage, not a warning."""
    from apps.backends.models import Backend

    raised = 0
    for backend in Backend.objects.filter(is_active=True).prefetch_related("instances"):
        instances = [i for i in backend.instances.all() if i.is_active]
        if not instances:
            continue

        healthy = [i for i in instances if i.is_healthy]
        key = fingerprint("backend-health", backend.pk)

        if not healthy:
            _settle(
                key, True,
                severity=Severity.CRITICAL,
                title=f"Every instance of {backend.name} is failing its health check",
                body=(
                    f"All {len(instances)} instances are unhealthy, so any route "
                    "pointing at this backend has nowhere to send traffic."
                ),
                runbook=(
                    "Check the Backends page for what each probe returned. A "
                    "whole backend failing at once is more often the health "
                    "check asking the wrong question -- wrong path, wrong "
                    "expected status, missing Host header -- than every "
                    "instance dying together."
                ),
                labels={"backend": backend.name, "instances": len(instances)},
            )
            raised += 1
        elif len(healthy) < len(instances):
            _settle(
                key, True,
                severity=Severity.WARNING,
                title=(
                    f"{len(instances) - len(healthy)} of {len(instances)} "
                    f"instances of {backend.name} are unhealthy"
                ),
                body="The backend is still serving, with less capacity than it has.",
                runbook="Check the Backends page for which instances and why.",
                labels={"backend": backend.name,
                        "healthy": len(healthy), "total": len(instances)},
            )
            raised += 1
        else:
            alert_resolve(key=key)

    return raised


def check_backups() -> int:
    """
    Whether anything on disk is known to restore.

    The condition worth alerting on is not "the backup failed" -- that one is
    loud. It is "nothing has been proven restorable for days", which is silent
    by nature and only discovered when someone needs one.
    """
    from apps.backups.services import backup_health

    health = backup_health()
    key = fingerprint("backup-health")

    if not health["warnings"]:
        alert_resolve(key=key)
        return 0

    serious = [w for w in health["warnings"] if "not encrypted" not in w]
    if not serious:
        alert_resolve(key=key)
        return 0

    _settle(
        key, True,
        severity=Severity.CRITICAL if health["newest_verified_at"] is None else Severity.WARNING,
        title="Backups are not in a state anyone should rely on",
        body="\n".join(serious),
        runbook=(
            "The Backups page shows every run and why it failed. `make backup` "
            "takes one now and verifies it by restoring into a scratch "
            "database, which is the only way to find out whether backups work "
            "on this host before needing them to."
        ),
        labels={"verified_backups": health["total_on_disk"]},
    )
    return 1


def check_deploys() -> int:
    """A configuration the gateway refused, which means it is running an old one."""
    from apps.routing.models import ConfigDeployLog

    latest = ConfigDeployLog.objects.order_by("-created_at").first()
    key = fingerprint("deploy-failed")

    if latest is None or latest.status != ConfigDeployLog.Status.FAILED:
        alert_resolve(key=key)
        return 0

    _settle(
        key, True,
        severity=Severity.WARNING,
        title="The last configuration deploy failed",
        body=(
            "Nginx is still serving the previous configuration, so traffic is "
            "unaffected -- but nothing changed in the panel since then is live."
        ),
        runbook=(
            "The Deploys page has the output. A failed `nginx -t` is restored "
            "from backup automatically, so the gateway is in a known-good "
            "state; the change that caused it is not."
        ),
        labels={"deploy_id": latest.pk},
    )
    return 1


def check_disk() -> int:
    """Disk, because every one of these systems stops when it runs out."""
    import shutil

    raised = 0
    for label, path in (
        ("backups", settings.BACKUP_DIR),
        ("cache", settings.NGINX_CACHE_DIR),
    ):
        key = fingerprint("disk-space", label)
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue

        free_pct = usage.free / usage.total * 100 if usage.total else 100
        if free_pct >= settings.ALERT_DISK_WARNING_PERCENT:
            alert_resolve(key=key)
            continue

        _settle(
            key, True,
            severity=(
                Severity.CRITICAL
                if free_pct < settings.ALERT_DISK_CRITICAL_PERCENT
                else Severity.WARNING
            ),
            title=f"The filesystem holding {label} is {100 - free_pct:.0f}% full",
            body=f"{usage.free / 1e9:.1f} GB free of {usage.total / 1e9:.1f} GB.",
            runbook=(
                "Backups prune on a schedule; the cache evicts on its own. A "
                "disk filling anyway usually means something else on this host "
                "is growing, most often container logs."
            ),
            labels={"path": str(path), "free_percent": round(free_pct, 1)},
        )
        raised += 1

    return raised


def run_all_checks() -> dict:
    """
    Every control-plane check, each isolated from the others.

    One check raising an exception must not stop the rest. They are
    independent questions, and the one that crashes is the least likely to be
    the one that mattered this minute.
    """
    # Named explicitly rather than read off the function. A check swapped out
    # -- by a test, or by anything that wraps it -- stops having a __name__,
    # and the error handler whose whole job is to survive a failing check
    # would be the thing that raised.
    checks = (
        ("check_certificates", check_certificates),
        ("check_backends", check_backends),
        ("check_backups", check_backups),
        ("check_deploys", check_deploys),
        ("check_disk", check_disk),
    )

    results = {}
    for name, check in checks:
        try:
            results[name] = check()
        except Exception as exc:
            logger.exception("Alert check %s failed", name)
            results[name] = f"error: {exc}"
    return results
