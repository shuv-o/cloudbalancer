"""
Certificate models.

A Certificate is the TLS material for one Domain. It can come from three
places: an ACME provider (Let's Encrypt via certbot), a self-signed pair we
generate locally, or a PEM pair the operator pasted in.

Whichever the source, the rendered Nginx server block reads the same two
paths, so the rest of the system does not care where a certificate came from.
"""
from django.db import models
from django.utils import timezone


class AcmeAccount(models.Model):
    """
    An ACME account registration.

    One row per (directory, email). Certbot keeps the real account key on disk
    under its config dir; this model records which account is in use so the GUI
    can show it and so we never register twice.
    """

    class Directory(models.TextChoices):
        LETSENCRYPT = (
            "https://acme-v02.api.letsencrypt.org/directory",
            "Let's Encrypt (production)",
        )
        LETSENCRYPT_STAGING = (
            "https://acme-staging-v02.api.letsencrypt.org/directory",
            "Let's Encrypt (staging)",
        )
        ZEROSSL = ("https://acme.zerossl.com/v2/DV90", "ZeroSSL")

    email = models.EmailField(
        help_text="Contact address for expiry warnings from the CA",
    )
    directory_url = models.URLField(
        max_length=255,
        choices=Directory.choices,
        default=Directory.LETSENCRYPT,
    )
    agreed_to_tos = models.BooleanField(default=False)
    external_account_kid = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="EAB key ID, required by ZeroSSL and some private CAs",
    )
    external_account_hmac = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="EAB HMAC key, stored as given by the CA",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Used when a domain requests a certificate without naming an account",
    )
    registered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "email"]
        unique_together = [("email", "directory_url")]

    def __str__(self) -> str:
        return f"{self.email} @ {self.get_directory_url_display()}"

    @property
    def is_staging(self) -> bool:
        return self.directory_url == self.Directory.LETSENCRYPT_STAGING


class Certificate(models.Model):
    """
    TLS material for a single domain.

    `cert_path` and `key_path` are what the Nginx template renders. For ACME
    certificates they point into certbot's live directory, which certbot
    updates in place on renewal — so a renewal needs only a reload, not a
    re-render.
    """

    class Issuer(models.TextChoices):
        ACME = "acme", "Let's Encrypt / ACME"
        SELF_SIGNED = "self_signed", "Self-signed"
        MANUAL = "manual", "Uploaded manually"

    class Status(models.TextChoices):
        PENDING = "pending", "Not issued yet"
        REQUESTING = "requesting", "Requesting"
        ACTIVE = "active", "Active"
        RENEWING = "renewing", "Renewing"
        EXPIRED = "expired", "Expired"
        FAILED = "failed", "Failed"

    domain = models.OneToOneField(
        "domains.Domain",
        on_delete=models.CASCADE,
        related_name="certificate",
    )
    acme_account = models.ForeignKey(
        AcmeAccount,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="certificates",
    )

    issuer = models.CharField(
        max_length=20,
        choices=Issuer.choices,
        default=Issuer.ACME,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    # Where Nginx reads the material from.
    cert_path = models.CharField(max_length=512, blank=True, default="")
    key_path = models.CharField(max_length=512, blank=True, default="")
    chain_path = models.CharField(max_length=512, blank=True, default="")

    # Parsed out of the certificate itself by certificate_sync_metadata().
    subject_common_name = models.CharField(max_length=253, blank=True, default="")
    san_domains = models.JSONField(
        default=list,
        blank=True,
        help_text="Subject Alternative Names covered by this certificate",
    )
    serial_number = models.CharField(max_length=128, blank=True, default="")
    fingerprint_sha256 = models.CharField(max_length=95, blank=True, default="")
    not_before = models.DateTimeField(null=True, blank=True)
    not_after = models.DateTimeField(null=True, blank=True, db_index=True)

    # Renewal policy.
    auto_renew = models.BooleanField(default=True)
    renew_before_days = models.IntegerField(
        default=30,
        help_text="Start renewing this many days before expiry",
    )

    last_issued_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    attempt_count = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["domain__name"]

    def __str__(self) -> str:
        return f"{self.domain.name} [{self.status}]"

    @property
    def days_until_expiry(self) -> int | None:
        if self.not_after is None:
            return None
        return (self.not_after - timezone.now()).days

    @property
    def is_due_for_renewal(self) -> bool:
        """True when the certificate is inside its renewal window."""
        if not self.auto_renew or self.issuer != self.Issuer.ACME:
            return False
        remaining = self.days_until_expiry
        if remaining is None:
            return self.status in (self.Status.PENDING, self.Status.FAILED)
        return remaining <= self.renew_before_days

    @property
    def is_usable(self) -> bool:
        """True when Nginx can actually load this certificate right now."""
        return bool(
            self.cert_path
            and self.key_path
            and self.status in (self.Status.ACTIVE, self.Status.RENEWING)
        )
