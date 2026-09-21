"""
Certificate services — issuance, renewal, import.

ACME issuance shells out to certbot in webroot mode. Certbot writes the
challenge token into a directory that Nginx also mounts and serves at
/.well-known/acme-challenge/, so no port needs to be freed and no reload is
needed mid-challenge.

The ordering that avoids the usual chicken-and-egg problem:

  1. A new domain deploys as an HTTP-only server block that already serves
     the ACME challenge path.
  2. Issuance runs against that live block.
  3. On success the certificate is attached and the config is re-rendered,
     which is the first time a 443 listener appears for the domain.
"""
import logging
import re
import subprocess
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.certificates.models import AcmeAccount, Certificate
from apps.domains.models import Domain
from common.services import model_update

logger = logging.getLogger(__name__)

_DOMAIN_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")


class CertificateError(Exception):
    """Raised when issuance, renewal or import cannot complete."""


# ---------------------------------------------------------------------------
# ACME account
# ---------------------------------------------------------------------------

def acme_account_create(
    *,
    email: str,
    directory_url: str = AcmeAccount.Directory.LETSENCRYPT,
    agreed_to_tos: bool = False,
    external_account_kid: str = "",
    external_account_hmac: str = "",
    is_default: bool = False,
) -> AcmeAccount:
    """Register an ACME account. Certbot performs the actual registration."""
    if not agreed_to_tos:
        raise CertificateError(
            "The CA's subscriber agreement must be accepted before an account can be registered."
        )

    account = AcmeAccount(
        email=email.strip().lower(),
        directory_url=directory_url,
        agreed_to_tos=agreed_to_tos,
        external_account_kid=external_account_kid,
        external_account_hmac=external_account_hmac,
        is_default=is_default,
    )
    account.full_clean()

    with transaction.atomic():
        if is_default:
            AcmeAccount.objects.filter(is_default=True).update(is_default=False)
        account.save()

    _certbot_register(account=account)
    account.registered_at = timezone.now()
    account.save(update_fields=["registered_at"])
    return account


def acme_account_update(*, account: AcmeAccount, data: dict) -> AcmeAccount:
    fields = [
        "email",
        "directory_url",
        "agreed_to_tos",
        "external_account_kid",
        "external_account_hmac",
        "is_default",
    ]
    with transaction.atomic():
        if data.get("is_default"):
            AcmeAccount.objects.exclude(pk=account.pk).update(is_default=False)
        instance, _ = model_update(instance=account, fields=fields, data=data)
    return instance


def acme_account_delete(*, account: AcmeAccount) -> None:
    account.delete()


def acme_account_default() -> AcmeAccount | None:
    return AcmeAccount.objects.filter(is_default=True).first()


# ---------------------------------------------------------------------------
# Certbot invocation
# ---------------------------------------------------------------------------

def _certbot_base_args(*, account: AcmeAccount) -> list[str]:
    return [
        settings.CERTBOT_BIN,
        "--non-interactive",
        "--config-dir", settings.LETSENCRYPT_DIR,
        "--work-dir", settings.CERTBOT_WORK_DIR,
        "--logs-dir", settings.CERTBOT_LOG_DIR,
        "--server", account.directory_url,
    ]


def _certbot_run(args: list[str], *, timeout: int = 180) -> tuple[bool, str]:
    """Run certbot and return (success, combined output)."""
    printable = " ".join(args)
    logger.info("certbot: %s", printable)
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, (
            f"certbot executable not found at {settings.CERTBOT_BIN}. "
            "Install certbot in the management image or set CERTBOT_BIN."
        )
    except subprocess.TimeoutExpired:
        return False, f"certbot timed out after {timeout}s"

    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def _certbot_register(*, account: AcmeAccount) -> None:
    args = _certbot_base_args(account=account) + [
        "register",
        "--agree-tos",
        "-m", account.email,
    ]
    if account.external_account_kid:
        args += [
            "--eab-kid", account.external_account_kid,
            "--eab-hmac-key", account.external_account_hmac,
        ]

    ok, output = _certbot_run(args)
    # Re-registering an existing account is not an error for our purposes.
    if not ok and "already" not in output.lower():
        raise CertificateError(f"ACME registration failed: {output}")


# ---------------------------------------------------------------------------
# Issuance
# ---------------------------------------------------------------------------

def certificate_ensure(*, domain: Domain) -> Certificate:
    """Return the domain's certificate row, creating an empty one if needed."""
    certificate = Certificate.objects.filter(domain=domain).first()
    if certificate is None:
        certificate = Certificate.objects.create(domain=domain)
    return certificate


def _validate_acme_domain(name: str) -> None:
    if not _DOMAIN_RE.match(name):
        raise CertificateError(
            f"'{name}' is not a public hostname, so a certificate authority cannot validate it. "
            "Use a self-signed certificate for local or internal names."
        )


def certificate_issue_acme(
    *,
    domain: Domain,
    account: AcmeAccount | None = None,
    extra_domains: list[str] | None = None,
    force: bool = False,
) -> Certificate:
    """
    Obtain a certificate for `domain` over the ACME HTTP-01 challenge.

    Requires the domain's HTTP server block to already be live, which is what
    a deploy does as soon as the domain is created.
    """
    account = account or acme_account_default()
    if account is None:
        raise CertificateError(
            "No ACME account is configured. Add one under SSL before requesting certificates."
        )

    names = [domain.name] + [d.strip().lower() for d in (extra_domains or []) if d.strip()]
    for name in names:
        _validate_acme_domain(name)

    certificate = certificate_ensure(domain=domain)
    certificate.issuer = Certificate.Issuer.ACME
    certificate.acme_account = account
    certificate.status = (
        Certificate.Status.RENEWING
        if certificate.status == Certificate.Status.ACTIVE
        else Certificate.Status.REQUESTING
    )
    certificate.last_attempt_at = timezone.now()
    certificate.attempt_count += 1
    certificate.save(
        update_fields=[
            "issuer", "acme_account", "status",
            "last_attempt_at", "attempt_count", "updated_at",
        ]
    )

    webroot = Path(settings.ACME_WEBROOT_DIR)
    webroot.mkdir(parents=True, exist_ok=True)

    args = _certbot_base_args(account=account) + [
        "certonly",
        "--webroot",
        "--webroot-path", str(webroot),
        "--cert-name", domain.name,
        "--key-type", settings.ACME_KEY_TYPE,
        "--agree-tos",
        "-m", account.email,
        "--keep-until-expiring",
    ]
    if force:
        args.append("--force-renewal")
    for name in names:
        args += ["-d", name]

    ok, output = _certbot_run(args, timeout=settings.CERTBOT_TIMEOUT)

    if not ok:
        certificate.status = Certificate.Status.FAILED
        certificate.last_error = _summarise_certbot_error(output)
        certificate.save(update_fields=["status", "last_error", "updated_at"])
        logger.error("Certificate issuance failed for %s: %s", domain.name, output)
        return certificate

    live_dir = Path(settings.LETSENCRYPT_DIR) / "live" / domain.name
    certificate.cert_path = str(live_dir / "fullchain.pem")
    certificate.key_path = str(live_dir / "privkey.pem")
    certificate.chain_path = str(live_dir / "chain.pem")
    certificate.status = Certificate.Status.ACTIVE
    certificate.last_issued_at = timezone.now()
    certificate.last_error = ""
    certificate.save(
        update_fields=[
            "cert_path", "key_path", "chain_path",
            "status", "last_issued_at", "last_error", "updated_at",
        ]
    )

    certificate_sync_metadata(certificate=certificate)
    _attach_to_domain(certificate=certificate)
    logger.info("Certificate issued for %s", domain.name)
    return certificate


def _summarise_certbot_error(output: str) -> str:
    """
    Pull the operator-actionable line out of certbot's output.

    Certbot is verbose; the dashboard shows one line, and the full text stays
    in the container logs.
    """
    interesting = [
        line.strip()
        for line in output.splitlines()
        if any(
            marker in line
            for marker in ("Detail:", "Domain:", "Type:", "error:", "Error:", "Problem")
        )
    ]
    if interesting:
        return " / ".join(interesting[:4])[:1000]
    return output[-1000:] if output else "certbot failed without output"


# ---------------------------------------------------------------------------
# Self-signed
# ---------------------------------------------------------------------------

def certificate_generate_self_signed(
    *,
    domain: Domain,
    valid_days: int = 825,
) -> Certificate:
    """
    Generate a self-signed P-256 certificate.

    Useful for internal hostnames a public CA will not validate, and to get a
    443 listener up before DNS has propagated.
    """
    ssl_dir = Path(settings.NGINX_SSL_DIR) / domain.name
    ssl_dir.mkdir(parents=True, exist_ok=True)

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(dt_timezone.utc)
    subject = x509.Name([
        x509.NameAttribute(x509.NameOID.COMMON_NAME, domain.name),
        x509.NameAttribute(x509.NameOID.ORGANIZATION_NAME, settings.SELF_SIGNED_ORG),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain.name)]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = ssl_dir / "fullchain.pem"
    key_path = ssl_dir / "privkey.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)

    certificate = certificate_ensure(domain=domain)
    certificate.issuer = Certificate.Issuer.SELF_SIGNED
    certificate.status = Certificate.Status.ACTIVE
    certificate.cert_path = str(cert_path)
    certificate.key_path = str(key_path)
    certificate.chain_path = ""
    certificate.auto_renew = False
    certificate.last_issued_at = timezone.now()
    certificate.last_error = ""
    certificate.save()

    certificate_sync_metadata(certificate=certificate)
    _attach_to_domain(certificate=certificate)
    return certificate


# ---------------------------------------------------------------------------
# Manual import
# ---------------------------------------------------------------------------

def certificate_import(
    *,
    domain: Domain,
    cert_pem: str,
    key_pem: str,
) -> Certificate:
    """Store an operator-supplied PEM pair after checking that they match."""
    try:
        parsed = x509.load_pem_x509_certificate(cert_pem.encode())
    except Exception as exc:
        raise CertificateError(f"The certificate is not valid PEM: {exc}") from exc

    try:
        private_key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    except Exception as exc:
        raise CertificateError(f"The private key is not valid unencrypted PEM: {exc}") from exc

    if private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ) != parsed.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ):
        raise CertificateError("The private key does not match the certificate.")

    ssl_dir = Path(settings.NGINX_SSL_DIR) / domain.name
    ssl_dir.mkdir(parents=True, exist_ok=True)
    cert_path = ssl_dir / "fullchain.pem"
    key_path = ssl_dir / "privkey.pem"
    cert_path.write_text(cert_pem, encoding="utf-8")
    key_path.write_text(key_pem, encoding="utf-8")
    key_path.chmod(0o600)

    certificate = certificate_ensure(domain=domain)
    certificate.issuer = Certificate.Issuer.MANUAL
    certificate.status = Certificate.Status.ACTIVE
    certificate.cert_path = str(cert_path)
    certificate.key_path = str(key_path)
    certificate.auto_renew = False
    certificate.last_issued_at = timezone.now()
    certificate.last_error = ""
    certificate.save()

    certificate_sync_metadata(certificate=certificate)
    _attach_to_domain(certificate=certificate)
    return certificate


# ---------------------------------------------------------------------------
# Metadata + lifecycle
# ---------------------------------------------------------------------------

def certificate_sync_metadata(*, certificate: Certificate) -> Certificate:
    """Read validity dates and names out of the certificate file on disk."""
    path = Path(certificate.cert_path)
    if not certificate.cert_path or not path.exists():
        return certificate

    try:
        parsed = x509.load_pem_x509_certificate(path.read_bytes())
    except Exception as exc:
        logger.warning("Could not parse %s: %s", path, exc)
        return certificate

    try:
        san = parsed.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        san = []

    common_names = parsed.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)

    certificate.subject_common_name = common_names[0].value if common_names else ""
    certificate.san_domains = list(san)
    certificate.serial_number = format(parsed.serial_number, "x")
    certificate.fingerprint_sha256 = parsed.fingerprint(hashes.SHA256()).hex(":")
    certificate.not_before = parsed.not_valid_before_utc
    certificate.not_after = parsed.not_valid_after_utc

    if certificate.not_after <= timezone.now():
        certificate.status = Certificate.Status.EXPIRED

    certificate.save(
        update_fields=[
            "subject_common_name", "san_domains", "serial_number",
            "fingerprint_sha256", "not_before", "not_after",
            "status", "updated_at",
        ]
    )
    return certificate


def _attach_to_domain(*, certificate: Certificate) -> None:
    """Point the domain at the certificate and switch its server block to TLS."""
    domain = certificate.domain
    domain.ssl_enabled = True
    domain.ssl_cert_path = certificate.cert_path
    domain.ssl_key_path = certificate.key_path
    domain.save(update_fields=["ssl_enabled", "ssl_cert_path", "ssl_key_path", "updated_at"])


def certificate_update(*, certificate: Certificate, data: dict) -> Certificate:
    fields = ["auto_renew", "renew_before_days", "acme_account"]
    instance, _ = model_update(instance=certificate, fields=fields, data=data)
    return instance


def certificate_delete(*, certificate: Certificate) -> None:
    """
    Remove a certificate and take the domain back to plain HTTP.

    The material on disk is left alone: certbot owns its own directory, and
    keeping uploaded PEMs means a mistaken delete is recoverable.
    """
    domain = certificate.domain
    domain.ssl_enabled = False
    domain.ssl_cert_path = ""
    domain.ssl_key_path = ""
    domain.save(update_fields=["ssl_enabled", "ssl_cert_path", "ssl_key_path", "updated_at"])
    certificate.delete()


def certificate_renew(*, certificate: Certificate, force: bool = False) -> Certificate:
    """Renew one ACME certificate."""
    if certificate.issuer != Certificate.Issuer.ACME:
        raise CertificateError(
            f"{certificate.domain.name} uses a {certificate.get_issuer_display().lower()} "
            "certificate, which this gateway cannot renew on its own."
        )
    return certificate_issue_acme(
        domain=certificate.domain,
        account=certificate.acme_account,
        force=force,
    )
