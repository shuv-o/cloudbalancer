"""
Celery tasks for certificate issuance and renewal.

Issuance talks to a remote CA and can take tens of seconds, so it never runs
inside a request. Every task that changes certificate material ends by asking
the gateway to re-render and reload, because a new certificate means a new
443 listener.
"""
import logging

from celery import shared_task

from apps.certificates.models import Certificate
from apps.certificates.selectors import certificate_list_due_for_renewal
from apps.certificates.services import (
    CertificateError,
    certificate_generate_self_signed,
    certificate_issue_acme,
    certificate_renew,
    certificate_sync_metadata,
)
from apps.domains.models import Domain

logger = logging.getLogger(__name__)


def _request_deploy() -> None:
    """Ask the gateway to pick up the new certificate paths."""
    from apps.gateway.tasks import request_config_deploy

    request_config_deploy()


@shared_task(name="certificates.issue_acme")
def async_certificate_issue(
    domain_id: int,
    acme_account_id: int | None = None,
    extra_domains: list[str] | None = None,
    force: bool = False,
) -> dict:
    """Request a certificate for one domain over ACME."""
    from apps.certificates.models import AcmeAccount

    try:
        domain = Domain.objects.get(pk=domain_id)
    except Domain.DoesNotExist:
        return {"error": f"Domain {domain_id} no longer exists"}

    account = None
    if acme_account_id:
        account = AcmeAccount.objects.filter(pk=acme_account_id).first()

    try:
        certificate = certificate_issue_acme(
            domain=domain,
            account=account,
            extra_domains=extra_domains,
            force=force,
        )
    except CertificateError as exc:
        logger.error("Issuance rejected for %s: %s", domain.name, exc)
        return {"domain": domain.name, "status": "failed", "error": str(exc)}

    if certificate.status == Certificate.Status.ACTIVE:
        _request_deploy()

    return {
        "domain": domain.name,
        "status": certificate.status,
        "not_after": certificate.not_after.isoformat() if certificate.not_after else None,
        "error": certificate.last_error,
    }


@shared_task(name="certificates.generate_self_signed")
def async_certificate_self_signed(domain_id: int, valid_days: int = 825) -> dict:
    try:
        domain = Domain.objects.get(pk=domain_id)
    except Domain.DoesNotExist:
        return {"error": f"Domain {domain_id} no longer exists"}

    certificate = certificate_generate_self_signed(domain=domain, valid_days=valid_days)
    _request_deploy()
    return {"domain": domain.name, "status": certificate.status}


@shared_task(name="certificates.renew_one")
def async_certificate_renew(certificate_id: int, force: bool = False) -> dict:
    try:
        certificate = Certificate.objects.select_related("domain").get(pk=certificate_id)
    except Certificate.DoesNotExist:
        return {"error": f"Certificate {certificate_id} no longer exists"}

    try:
        certificate = certificate_renew(certificate=certificate, force=force)
    except CertificateError as exc:
        return {"domain": certificate.domain.name, "status": "failed", "error": str(exc)}

    if certificate.status == Certificate.Status.ACTIVE:
        _request_deploy()

    return {"domain": certificate.domain.name, "status": certificate.status}


@shared_task(name="certificates.renew_due")
def async_certificate_renew_due() -> dict:
    """
    Daily sweep: renew everything inside its renewal window.

    Runs one reload at the end rather than one per certificate, so a batch of
    renewals costs a single worker cycle.
    """
    renewed, failed, skipped = [], [], []

    for certificate in certificate_list_due_for_renewal():
        if not certificate.is_due_for_renewal:
            skipped.append(certificate.domain.name)
            continue
        try:
            result = certificate_renew(certificate=certificate)
        except CertificateError as exc:
            logger.error("Renewal rejected for %s: %s", certificate.domain.name, exc)
            failed.append({"domain": certificate.domain.name, "error": str(exc)})
            continue

        if result.status == Certificate.Status.ACTIVE:
            renewed.append(certificate.domain.name)
        else:
            failed.append({"domain": certificate.domain.name, "error": result.last_error})

    if renewed:
        _request_deploy()

    logger.info("Renewal sweep: %d renewed, %d failed, %d skipped",
                len(renewed), len(failed), len(skipped))
    return {"renewed": renewed, "failed": failed, "skipped": skipped}


@shared_task(name="certificates.sync_metadata")
def async_certificate_sync_all() -> dict:
    """
    Re-read every certificate file and refresh expiry dates.

    Catches certificates renewed outside this system (a certbot cron on the
    host, for instance) and flags ones that expired while nothing was watching.
    """
    synced = 0
    for certificate in Certificate.objects.exclude(cert_path=""):
        certificate_sync_metadata(certificate=certificate)
        synced += 1
    return {"synced": synced}
