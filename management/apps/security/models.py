"""
Security models for the control panel.

The panel configures the gateway, so reaching it is equivalent to controlling
every hostname the gateway serves. On a loopback-only bind that is protected by
whoever can reach the machine. Once it answers on a public name, the protection
has to be built here instead, in layers that fail independently:

    network    an address allowlist, evaluated before anything else runs
    transport  TLS, and optionally a client certificate
    identity   a password, then a second factor
    rate       a cap on guesses, per address and per account
    record     an audit trail of everything that changed

No single one of these is enough on its own, and the point of having all five
is that defeating one still leaves four.
"""
import ipaddress

from django.db import models
from django.utils import timezone


class PanelAccessPolicy(models.Model):
    """
    How the control panel may be reached. A single row.

    Kept in the database rather than in environment variables so it can be
    changed from the panel and audited like anything else — but the loopback
    listener on port 8081 is deliberately outside this policy, so a mistake
    here can never lock an operator out of the machine entirely.
    """

    # ---- Where it answers -------------------------------------------------
    panel_domain = models.CharField(
        max_length=253,
        blank=True,
        default="",
        help_text="Public hostname for the panel, e.g. control.example.com. "
                  "Blank keeps the panel on the loopback port only.",
    )
    is_published = models.BooleanField(
        default=False,
        help_text="Serve the panel on that hostname. Off until a certificate exists.",
    )

    # ---- Network ----------------------------------------------------------
    ip_allowlist = models.JSONField(
        default=list,
        blank=True,
        help_text="CIDR ranges allowed to reach the panel. Empty means any address, "
                  "which is the weakest setting here.",
    )

    # ---- Transport --------------------------------------------------------
    require_mtls = models.BooleanField(
        default=False,
        help_text="Require a client certificate signed by the CA below. The strongest "
                  "control available: an attacker without the certificate never reaches "
                  "the login form at all.",
    )
    mtls_ca_path = models.CharField(
        max_length=512,
        blank=True,
        default="",
        help_text="PEM bundle of the CA that signs operator certificates",
    )
    hsts_seconds = models.IntegerField(
        default=31536000,
        help_text="Strict-Transport-Security max-age for the panel hostname",
    )

    # ---- Identity ---------------------------------------------------------
    require_totp = models.BooleanField(
        default=True,
        help_text="Require an authenticator code as well as a password. A password "
                  "alone is one leak away from full control of every domain.",
    )

    # ---- Rate ------------------------------------------------------------
    login_rate_per_minute = models.IntegerField(
        default=10,
        help_text="Sign-in attempts allowed per minute, per client address",
    )
    api_rate_per_second = models.IntegerField(
        default=40,
        help_text="Requests per second allowed per client address across the panel",
    )
    lockout_threshold = models.IntegerField(
        default=5,
        help_text="Failed attempts before an account is locked. 0 disables locking.",
    )
    lockout_minutes = models.IntegerField(
        default=15,
        help_text="How long an account stays locked",
    )

    # ---- Sessions ---------------------------------------------------------
    session_idle_minutes = models.IntegerField(
        default=60,
        help_text="Sign out after this long with no activity",
    )
    session_max_hours = models.IntegerField(
        default=12,
        help_text="Sign out this long after signing in, active or not",
    )

    # ---- Surface ----------------------------------------------------------
    expose_django_admin = models.BooleanField(
        default=False,
        help_text="Serve the Django admin on the public hostname. It is a much larger "
                  "surface than the panel's own API and is blocked by default.",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "panel access policy"
        verbose_name_plural = "panel access policy"

    def __str__(self) -> str:
        if not self.is_published:
            return "Panel on loopback only"
        return f"Panel published at {self.panel_domain}"

    @classmethod
    def load(cls) -> "PanelAccessPolicy":
        """The policy row, created with safe defaults if it does not exist."""
        policy, _ = cls.objects.get_or_create(pk=1)
        return policy

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @property
    def normalised_allowlist(self) -> list[str]:
        """Valid CIDRs only. A malformed entry is dropped rather than rendered."""
        valid = []
        for entry in self.ip_allowlist or []:
            try:
                valid.append(str(ipaddress.ip_network(entry.strip(), strict=False)))
            except ValueError:
                continue
        return valid

    @property
    def exposure_warnings(self) -> list[str]:
        """
        What is weak about the current policy, in plain terms.

        Shown in the panel rather than buried in documentation, because the
        settings that matter most are the ones nobody revisits.
        """
        warnings = []
        if not self.is_published:
            return warnings
        if not self.normalised_allowlist and not self.require_mtls:
            warnings.append(
                "The panel accepts connections from any address on the internet. "
                "An allowlist or a client certificate would mean an attacker never "
                "reaches the sign-in form."
            )
        if not self.require_totp:
            warnings.append(
                "A password is the only thing protecting the panel. One reused or "
                "leaked password is full control of every domain this gateway serves."
            )
        if self.expose_django_admin:
            warnings.append(
                "The Django admin is reachable publicly. It is a far larger surface "
                "than the panel's own API and grants the same powers."
            )
        if self.lockout_threshold == 0:
            warnings.append("Accounts are never locked, so passwords can be guessed indefinitely.")
        return warnings


class TotpDevice(models.Model):
    """
    An authenticator app bound to one operator.

    `last_used_step` is what stops a code being replayed: a TOTP code stays
    valid for its whole window, so an attacker who observes one has thirty
    seconds to use it unless the step it belongs to is refused twice.
    """

    user = models.OneToOneField(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="totp_device",
    )
    secret = models.CharField(max_length=64)
    confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set once the operator has proved the app is working",
    )
    last_used_step = models.BigIntegerField(
        default=0,
        help_text="Time step of the last accepted code, so it cannot be reused",
    )
    recovery_codes = models.JSONField(
        default=list,
        blank=True,
        help_text="Hashed single-use codes for when the authenticator is lost",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        state = "confirmed" if self.confirmed_at else "awaiting confirmation"
        return f"Authenticator for {self.user.get_username()} ({state})"

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None

    @property
    def recovery_codes_remaining(self) -> int:
        return len(self.recovery_codes or [])


class LoginAttempt(models.Model):
    """
    Every sign-in attempt, successful or not.

    Feeds account lockout, and gives an operator the answer to "is someone
    trying?" without shelling into a log file.
    """

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Signed in"
        BAD_PASSWORD = "bad_password", "Wrong password"
        BAD_TOTP = "bad_totp", "Wrong authenticator code"
        LOCKED = "locked", "Account locked"
        BLOCKED_IP = "blocked_ip", "Address not allowed"
        INACTIVE = "inactive", "Account disabled"

    username = models.CharField(max_length=254, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    outcome = models.CharField(max_length=20, choices=Outcome.choices, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["username", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.username} from {self.ip_address}: {self.outcome}"

    @property
    def succeeded(self) -> bool:
        return self.outcome == self.Outcome.SUCCESS


class AccountLock(models.Model):
    """A temporary lock after repeated failures."""

    username = models.CharField(max_length=254, unique=True, db_index=True)
    locked_until = models.DateTimeField()
    failed_count = models.IntegerField(default=0)
    last_ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.username} locked until {self.locked_until}"

    @property
    def is_active(self) -> bool:
        return self.locked_until > timezone.now()

    @property
    def minutes_remaining(self) -> int:
        if not self.is_active:
            return 0
        return max(1, round((self.locked_until - timezone.now()).total_seconds() / 60))


class AuditEvent(models.Model):
    """
    A record of everything that changed the gateway, and who changed it.

    Deliberately append-only from the application's side: there is no service
    that edits or deletes a row here. An audit trail an attacker can tidy up
    after themselves is not an audit trail.
    """

    actor = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    actor_name = models.CharField(
        max_length=254,
        blank=True,
        default="",
        help_text="Kept separately so the record survives the account being deleted",
    )
    action = models.CharField(max_length=16, help_text="HTTP method")
    path = models.CharField(max_length=512)
    status_code = models.IntegerField()
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    summary = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="What happened, in the panel's own words",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.actor_name or 'anonymous'} {self.action} {self.path}"


class TrafficProtectionPolicy(models.Model):
    """
    Per-address limits applied to every public domain. A single row.

    Worth being precise about what this is and is not. It bounds what one
    address can consume, which stops a single host, a scraper or a small
    botnet from exhausting workers or backends. It does nothing about a
    volumetric flood: by the time 10 Gbit/s of packets arrive, the uplink is
    already full and Nginx never sees them. That problem is solved upstream,
    by a provider who can absorb it, and no setting here substitutes for one.

    Route-level limits stay available for tighter caps on expensive paths;
    these are the floor beneath them.
    """

    enabled = models.BooleanField(
        default=True,
        help_text="Apply these limits to every public domain",
    )

    per_ip_connections = models.IntegerField(
        default=64,
        help_text=(
            "Concurrent connections one address may hold open. Low enough to "
            "bound a slow-connection attack, high enough not to break a browser "
            "opening several tabs behind one office address."
        ),
    )
    per_ip_requests_per_second = models.IntegerField(
        default=50,
        help_text="Sustained request rate allowed from one address",
    )
    per_ip_burst = models.IntegerField(
        default=100,
        help_text="How far above that rate a short burst may go before requests are refused",
    )

    # Slow-request defences. An attacker holding thousands of connections open
    # while trickling a request costs almost nothing to mount and exhausts a
    # worker pool, so the timeouts are deliberately short.
    client_header_timeout = models.IntegerField(
        default=10,
        help_text="Seconds allowed to send request headers",
    )
    client_body_timeout = models.IntegerField(
        default=10,
        help_text="Seconds allowed between chunks of a request body",
    )

    # Automatic banning is deliberately absent: a log-tailing loop in Python is
    # a poor imitation of fail2ban or CrowdSec, both of which already do this
    # properly. The denylist below is the operator's own, and those tools can
    # write into it.
    denylist_enabled = models.BooleanField(
        default=True,
        help_text="Refuse blocked addresses without a response, before any other work",
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "traffic protection policy"
        verbose_name_plural = "traffic protection policy"

    def __str__(self) -> str:
        if not self.enabled:
            return "Per-address limits off"
        return (
            f"{self.per_ip_requests_per_second} req/s and "
            f"{self.per_ip_connections} connections per address"
        )

    @classmethod
    def load(cls) -> "TrafficProtectionPolicy":
        policy, _ = cls.objects.get_or_create(pk=1)
        return policy

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)


class BlockedAddress(models.Model):
    """
    An address or range refused at the gateway.

    Rendered into a `geo` table and answered with 444, which closes the
    connection without sending a response. An attacker learns nothing and the
    gateway spends almost nothing.

    Populated by an operator, or by fail2ban and similar through the API.
    """

    class Reason(models.TextChoices):
        MANUAL = "manual", "Blocked by an operator"
        ABUSE = "abuse", "Repeated abuse"
        SCANNING = "scanning", "Scanning for vulnerabilities"
        CREDENTIAL_STUFFING = "credentials", "Guessing credentials"

    cidr = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="A single address, or a range like 203.0.113.0/24",
    )
    reason = models.CharField(max_length=20, choices=Reason.choices, default=Reason.MANUAL)
    note = models.CharField(max_length=255, blank=True, default="")
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Blocks expire unless this is empty, so a mistake heals itself",
    )
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="blocked_addresses",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "blocked addresses"

    def __str__(self) -> str:
        return f"{self.cidr} ({self.reason})"

    @property
    def is_active(self) -> bool:
        return self.expires_at is None or self.expires_at > timezone.now()
