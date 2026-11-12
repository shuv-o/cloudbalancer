"""
Field-level encryption for credentials the control plane stores.

A Cloudflare API token can edit every DNS record in an account. A TSIG key can
write to a zone. Neither is a password the panel verifies -- it has to hand
both back to certbot in cleartext -- so they cannot be hashed, and they sit in
a database that is now dumped nightly into an archive that may be copied
elsewhere.

So they are encrypted in the column. This does not protect against someone who
has the running system: the key is there, by necessity. It protects against
the copies -- a backup on another host, a dump sent to a colleague, a stray
pg_dump in a home directory -- which is where database credentials actually
leak from.

Fernet rather than anything assembled here: these values are small enough to
fit in memory whole, so the streaming construction in the backup code has no
advantage, and an audited authenticated primitive with no framing decisions
left to make is the right trade for a field.
"""
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

PREFIX = "enc:v1:"


class SecretError(Exception):
    """A stored credential could not be read."""


def _key() -> bytes:
    """
    The encryption key, from its own setting or derived from SECRET_KEY.

    A dedicated key is better, because SECRET_KEY gets rotated for reasons
    that have nothing to do with these credentials -- and a rotation that
    silently made every DNS provider unusable would be discovered at the next
    renewal, weeks later.

    Deriving from SECRET_KEY is the fallback rather than an error because the
    alternative is refusing to start on an installation that upgraded into
    this feature without reading the notes.
    """
    configured = getattr(settings, "CREDENTIAL_ENCRYPTION_KEY", "")
    if configured:
        material = configured.encode()
    else:
        material = ("cloudbalancer-credentials:" + settings.SECRET_KEY).encode()

    return base64.urlsafe_b64encode(hashlib.sha256(material).digest())


def encrypt(value: str) -> str:
    """Encrypt a credential for storage. An empty value stays empty."""
    if not value:
        return ""
    token = Fernet(_key()).encrypt(value.encode()).decode()
    return PREFIX + token


def decrypt(value: str) -> str:
    """
    Read a credential back.

    A value without the prefix is returned as-is. That is what makes turning
    this on non-breaking: rows written before encryption existed keep working
    and are re-encrypted the next time they are saved.
    """
    if not value:
        return ""
    if not value.startswith(PREFIX):
        return value

    try:
        return Fernet(_key()).decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise SecretError(
            "This credential cannot be decrypted. Either CREDENTIAL_ENCRYPTION_KEY "
            "changed, or SECRET_KEY changed while it was the key in use. The "
            "stored value cannot be recovered -- enter the credential again."
        ) from exc


class EncryptedTextField(models.TextField):
    """
    A text column whose contents are encrypted on the way in.

    Deliberately not searchable, orderable, or comparable: the ciphertext is
    different every time, so a lookup on it finds nothing. Anything that
    needs to be looked up does not belong in one of these.
    """

    def get_prep_value(self, value):
        if value is None:
            return None
        return encrypt(str(value))

    def from_db_value(self, value, expression, connection):
        if value is None:
            return None
        try:
            return decrypt(value)
        except SecretError:
            # Raising here would make the row unreadable, including the name
            # that tells an operator which credential to re-enter. The panel
            # shows this string; nothing is going to authenticate with it.
            logger.error("Could not decrypt a stored credential")
            return ""
