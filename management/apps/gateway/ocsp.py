"""
Whether a certificate can actually be stapled.

Nginx writes

    "ssl_stapling" ignored, no OCSP responder URL in the certificate

to the error log on every reload for a certificate that names no OCSP
responder, then serves the site normally without stapling. The warning is
harmless, but since Let's Encrypt retired its responders in 2025 and stopped
putting an OCSP URL in its leaves, it fires for every certificate this control
plane issues -- so the log fills with a line that can never be acted on, and a
log nobody can read is a log nobody checks.

Deleting `ssl_stapling` outright would silence it too, but a certificate
imported from a commercial CA may still advertise a responder and still benefit
from stapling. So the directive is emitted per certificate, decided by what the
leaf actually carries, rather than switched on everywhere and ignored by Nginx
most of the time.
"""
import logging
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

logger = logging.getLogger(__name__)


def has_ocsp_responder(cert_path: str) -> bool:
    """True when the leaf certificate names an OCSP responder to staple from.

    `cert_path` points at a fullchain file; `load_pem_x509_certificate` reads
    the first certificate in it, which is the leaf. The leaf is the right one
    to ask -- stapling serves the leaf's revocation status, and an intermediate
    carrying its own responder says nothing about that.

    Anything unreadable or unparseable answers False. Stapling is an
    optimisation and never a control -- no browser refuses a connection for a
    missing staple -- so when we cannot tell, the useful answer is to leave the
    directive out and stay quiet.
    """
    if not cert_path:
        return False

    try:
        leaf = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    except (OSError, ValueError) as exc:
        # Config renders on every deploy. A certificate file that is missing or
        # corrupt is a real problem, but `nginx -t` reports it in the same
        # deploy with a far better message, so this stays out of the way.
        logger.debug("No OCSP decision for %s: %s", cert_path, exc)
        return False

    try:
        access_descriptions = leaf.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        ).value
    except x509.ExtensionNotFound:
        return False

    return any(
        description.access_method == AuthorityInformationAccessOID.OCSP
        for description in access_descriptions
    )
