"""
Domain services — write operations.

Following the HackSoft Services pattern: plain functions, keyword-only args,
interface-agnostic (callable from views, management commands, Celery tasks).
"""
from django.db import transaction

from apps.domains.models import Domain
from common.services import model_update


def domain_create(
    *,
    name: str,
    description: str = "",
    ssl_enabled: bool = False,
    ssl_cert_path: str = "",
    ssl_key_path: str = "",
    force_ssl_redirect: bool = True,
    is_active: bool = True,
) -> Domain:
    """Create a new domain."""
    domain = Domain(
        name=name.strip().lower(),
        description=description,
        ssl_enabled=ssl_enabled,
        ssl_cert_path=ssl_cert_path,
        ssl_key_path=ssl_key_path,
        force_ssl_redirect=force_ssl_redirect,
        is_active=is_active,
    )
    domain.full_clean()
    domain.save()
    return domain


def domain_update(*, domain: Domain, data: dict) -> Domain:
    """
    Update a domain's fields.

    Only fields present in `data` are updated.
    """
    updatable_fields = [
        "name",
        "description",
        "ssl_enabled",
        "ssl_cert_path",
        "ssl_key_path",
        "force_ssl_redirect",
        "is_active",
    ]
    instance, _ = model_update(instance=domain, fields=updatable_fields, data=data)
    return instance


@transaction.atomic
def domain_delete(*, domain: Domain) -> None:
    """Delete a domain and all its routing rules (cascaded)."""
    domain.delete()
