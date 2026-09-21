"""
Security services — second factors, lockout, audit.
"""
import base64
import hashlib
import io
import ipaddress
import logging
import secrets
from datetime import timedelta

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.security.models import (
    AccountLock,
    AuditEvent,
    LoginAttempt,
    PanelAccessPolicy,
    TotpDevice,
)
from common.services import model_update

logger = logging.getLogger(__name__)

RECOVERY_CODE_COUNT = 10


class SecurityError(Exception):
    """Raised when a security operation is refused."""


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def policy_update(*, policy: PanelAccessPolicy, data: dict) -> PanelAccessPolicy:
    """
    Change how the panel may be reached.

    Publishing is refused without TLS material, because a panel served over
    plain HTTP hands its session cookie to anyone on the path — and that cookie
    is full control of every domain this gateway serves.
    """
    merged = {**_policy_as_dict(policy), **data}

    if merged.get("is_published"):
        domain_name = (merged.get("panel_domain") or "").strip()
        if not domain_name:
            raise SecurityError("A hostname is needed before the panel can be published.")

        from apps.domains.models import Domain

        domain = Domain.objects.filter(name=domain_name).first()
        if domain is None:
            raise SecurityError(
                f"Add {domain_name} as a domain first, so the gateway can answer for it "
                "and obtain a certificate."
            )
        if not (domain.ssl_enabled and domain.ssl_cert_path):
            raise SecurityError(
                f"{domain_name} has no certificate yet. Publishing the panel over plain "
                "HTTP would expose its session cookie to anyone on the network path."
            )

    if merged.get("require_mtls") and not (merged.get("mtls_ca_path") or "").strip():
        raise SecurityError(
            "Requiring a client certificate needs the CA bundle that signs them, "
            "otherwise Nginx will refuse every connection including yours."
        )

    fields = [
        "panel_domain", "is_published", "ip_allowlist",
        "require_mtls", "mtls_ca_path", "hsts_seconds",
        "require_totp",
        "login_rate_per_minute", "api_rate_per_second",
        "lockout_threshold", "lockout_minutes",
        "session_idle_minutes", "session_max_hours",
        "expose_django_admin",
    ]
    if "panel_domain" in data:
        data = {**data, "panel_domain": (data["panel_domain"] or "").strip().lower()}

    instance, _ = model_update(instance=policy, fields=fields, data=data)
    return instance


def _policy_as_dict(policy: PanelAccessPolicy) -> dict:
    return {
        "panel_domain": policy.panel_domain,
        "is_published": policy.is_published,
        "require_mtls": policy.require_mtls,
        "mtls_ca_path": policy.mtls_ca_path,
        "require_totp": policy.require_totp,
    }


# ---------------------------------------------------------------------------
# Address checks
# ---------------------------------------------------------------------------

def client_ip(request) -> str | None:
    """
    The client's address as the gateway saw it.

    X-Forwarded-For is only trusted because the panel is always reached through
    this project's own Nginx, which overwrites it. Reading it from a request
    that reached Django directly would let a caller name any address they like.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        # The left-most entry is the original client; the rest are proxies.
        candidate = forwarded.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    return request.META.get("REMOTE_ADDR")


def ip_is_allowed(*, policy: PanelAccessPolicy, address: str | None) -> bool:
    """
    Whether an address may reach the panel.

    Nginx enforces the same list and rejects first; this is the second check,
    so a misconfigured or bypassed front end does not become a way in.
    """
    allowlist = policy.normalised_allowlist
    if not allowlist:
        return True
    if address is None:
        return False
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in ipaddress.ip_network(entry) for entry in allowlist)


# ---------------------------------------------------------------------------
# Lockout
# ---------------------------------------------------------------------------

def lock_check(*, username: str) -> AccountLock | None:
    """Return the active lock on an account, if there is one."""
    lock = AccountLock.objects.filter(username=username).first()
    if lock and lock.is_active:
        return lock
    return None


@transaction.atomic
def lock_record_failure(*, username: str, ip_address: str | None) -> AccountLock | None:
    """
    Count a failure and lock the account once the threshold is crossed.

    Locking by account rather than only by address is what makes this useful
    against a distributed attempt, where every guess comes from somewhere new.
    """
    policy = PanelAccessPolicy.load()
    if policy.lockout_threshold <= 0:
        return None

    lock, _ = AccountLock.objects.select_for_update().get_or_create(
        username=username,
        defaults={"locked_until": timezone.now()},
    )

    # A lock that has expired starts the count again rather than resuming it.
    if not lock.is_active and lock.locked_until <= timezone.now():
        window_start = timezone.now() - timedelta(minutes=policy.lockout_minutes)
        if lock.updated_at < window_start:
            lock.failed_count = 0

    lock.failed_count += 1
    lock.last_ip = ip_address

    if lock.failed_count >= policy.lockout_threshold:
        lock.locked_until = timezone.now() + timedelta(minutes=policy.lockout_minutes)
        logger.warning(
            "Locked %s for %s minutes after %s failed attempts (last from %s)",
            username, policy.lockout_minutes, lock.failed_count, ip_address,
        )

    lock.save()
    return lock if lock.is_active else None


def lock_clear(*, username: str) -> None:
    """Forget the failures for an account after a successful sign-in."""
    AccountLock.objects.filter(username=username).delete()


def attempt_record(
    *,
    username: str,
    ip_address: str | None,
    user_agent: str,
    outcome: str,
) -> LoginAttempt:
    return LoginAttempt.objects.create(
        username=username[:254],
        ip_address=ip_address,
        user_agent=user_agent[:512],
        outcome=outcome,
    )


# ---------------------------------------------------------------------------
# Second factor
# ---------------------------------------------------------------------------

def _hash_recovery_code(code: str) -> str:
    """
    Recovery codes are stored hashed.

    They are as powerful as a password, so a database dump should not hand
    someone a working set of them.
    """
    salted = f"{settings.SECRET_KEY}:{code}".encode()
    return hashlib.sha256(salted).hexdigest()


def _generate_recovery_codes() -> tuple[list[str], list[str]]:
    """Return (plaintext codes shown once, hashes to store)."""
    codes = [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        for _ in range(RECOVERY_CODE_COUNT)
    ]
    return codes, [_hash_recovery_code(code) for code in codes]


def totp_begin_enrollment(*, user) -> dict:
    """
    Start binding an authenticator app to an account.

    The device is created unconfirmed. Until the operator proves the app is
    generating matching codes, nothing changes about how they sign in — which
    is what stops a half-finished setup from locking someone out.
    """
    device, _ = TotpDevice.objects.get_or_create(
        user=user,
        defaults={"secret": pyotp.random_base32()},
    )

    if device.is_confirmed:
        raise SecurityError(
            "An authenticator is already set up for this account. Remove it first "
            "if you are switching to a different device."
        )

    # A fresh secret on every restart, so an abandoned attempt leaves nothing
    # usable behind.
    device.secret = pyotp.random_base32()
    device.save(update_fields=["secret"])

    issuer = settings.TOTP_ISSUER
    uri = pyotp.TOTP(device.secret).provisioning_uri(
        name=user.get_username(), issuer_name=issuer
    )

    return {
        "secret": device.secret,
        "otpauth_uri": uri,
        "qr_svg": _qr_svg(uri),
        "issuer": issuer,
    }


def _qr_svg(data: str) -> str:
    """An inline SVG QR code, so the panel needs no image endpoint."""
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=10)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


def totp_confirm_enrollment(*, user, code: str) -> list[str]:
    """
    Finish enrollment and hand back the recovery codes, once.

    Returns the plaintext codes; only their hashes are kept. If the operator
    loses both the app and these, another superuser has to clear the device.
    """
    device = TotpDevice.objects.filter(user=user).first()
    if device is None:
        raise SecurityError("Start the setup before confirming it.")
    if device.is_confirmed:
        raise SecurityError("This authenticator is already confirmed.")

    if not pyotp.TOTP(device.secret).verify(code, valid_window=1):
        raise SecurityError(
            "That code did not match. Check the time on the device generating it — "
            "codes are time-based and a clock more than a minute out will never work."
        )

    plaintext, hashes = _generate_recovery_codes()
    device.confirmed_at = timezone.now()
    device.recovery_codes = hashes
    device.last_used_step = int(timezone.now().timestamp()) // 30
    device.save(update_fields=["confirmed_at", "recovery_codes", "last_used_step"])

    logger.info("Authenticator confirmed for %s", user.get_username())
    return plaintext


def totp_verify(*, user, code: str) -> bool:
    """
    Check a code, then make sure it cannot be used again.

    A TOTP code is valid for its whole 30-second step, so without recording the
    step an observed code can be replayed inside that window. Recovery codes are
    accepted here too, and each one is consumed on use.
    """
    device = TotpDevice.objects.filter(user=user).first()
    if device is None or not device.is_confirmed:
        return False

    code = code.strip().replace(" ", "")

    if _consume_recovery_code(device=device, code=code):
        logger.warning(
            "Recovery code used for %s; %s remaining",
            user.get_username(), device.recovery_codes_remaining,
        )
        return True

    totp = pyotp.TOTP(device.secret)
    if not totp.verify(code, valid_window=1):
        return False

    step = int(timezone.now().timestamp()) // 30
    # Accept the previous step too, but never the same one twice.
    if step <= device.last_used_step:
        logger.warning("Refused a replayed authenticator code for %s", user.get_username())
        return False

    device.last_used_step = step
    device.last_used_at = timezone.now()
    device.save(update_fields=["last_used_step", "last_used_at"])
    return True


def _consume_recovery_code(*, device: TotpDevice, code: str) -> bool:
    candidate = _hash_recovery_code(code.lower())
    remaining = list(device.recovery_codes or [])
    if candidate not in remaining:
        return False

    remaining.remove(candidate)
    device.recovery_codes = remaining
    device.last_used_at = timezone.now()
    device.save(update_fields=["recovery_codes", "last_used_at"])
    return True


def totp_disable(*, user, password: str, code: str) -> None:
    """
    Remove an authenticator.

    Requires both the password and a current code: if an attacker has taken over
    a session, this is the one operation that would let them weaken the account
    for later, so it asks for proof of both factors.
    """
    if not user.check_password(password):
        raise SecurityError("That password is not correct.")
    if not totp_verify(user=user, code=code):
        raise SecurityError("That authenticator code is not correct.")

    TotpDevice.objects.filter(user=user).delete()
    logger.warning("Authenticator removed for %s", user.get_username())


def totp_reset_for_user(*, actor, target_user) -> None:
    """
    Clear another operator's authenticator, for when a device is lost.

    Superusers only, and recorded — this is the break-glass path and it should
    be obvious in the audit trail when someone takes it.
    """
    if not actor.is_superuser:
        raise SecurityError("Only a superuser can reset another operator's authenticator.")
    if actor.pk == target_user.pk:
        raise SecurityError(
            "Use the remove option on your own account, which asks for your password "
            "and a current code."
        )

    TotpDevice.objects.filter(user=target_user).delete()
    logger.warning(
        "%s reset the authenticator for %s",
        actor.get_username(), target_user.get_username(),
    )


def user_requires_totp_setup(*, user) -> bool:
    """True when policy demands a second factor and this account has none."""
    if not PanelAccessPolicy.load().require_totp:
        return False
    device = TotpDevice.objects.filter(user=user).first()
    return device is None or not device.is_confirmed


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit_record(
    *,
    actor,
    action: str,
    path: str,
    status_code: int,
    ip_address: str | None,
    user_agent: str = "",
    summary: str = "",
) -> AuditEvent:
    return AuditEvent.objects.create(
        actor=actor if (actor and actor.is_authenticated) else None,
        actor_name=actor.get_username() if (actor and actor.is_authenticated) else "",
        action=action[:16],
        path=path[:512],
        status_code=status_code,
        ip_address=ip_address,
        user_agent=user_agent[:512],
        summary=summary[:255],
    )
