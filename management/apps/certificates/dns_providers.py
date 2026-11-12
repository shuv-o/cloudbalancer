"""
DNS-01 providers, and which zone each one is authoritative for.

Wildcard certificates cannot be issued over HTTP-01. Proving control of
a.example.com says nothing about b.example.com, so the only proof a CA will
accept for *.example.com is a TXT record at _acme-challenge.example.com. That
means writing to DNS, which means credentials, which means knowing which
nameserver to talk to for which zone.

Both kinds are here because both are real: some zones are at Cloudflare and
some are on BIND or Knot, and a gateway serving a university network will
have both for years. A domain does not choose a provider -- it is matched to
whichever one holds its zone, by longest suffix, so adding a domain under an
existing zone needs no configuration at all.

What each provider needs, written for the person typing it in:

    Cloudflare   an API token, not the global API key. Scope it to
                 Zone:DNS:Edit on the specific zones. A global key can
                 change billing.
    RFC2136      the nameserver to send updates to, a TSIG key name, its
                 algorithm and its secret. The zone must allow updates from
                 this key: in Knot, an acl with `update` action; in BIND, an
                 `allow-update { key ...; }` clause.
"""
import io
import logging
import os
import re
import stat
from pathlib import Path

from django.db import models

from common.secrets import EncryptedTextField

logger = logging.getLogger(__name__)

# RFC2136 TSIG algorithms certbot understands. Listed rather than free text
# because a typo here fails at the moment of renewal, months later, with an
# error from dnspython that names neither the zone nor the provider.
TSIG_ALGORITHMS = (
    "HMAC-SHA512",
    "HMAC-SHA384",
    "HMAC-SHA256",
    "HMAC-SHA224",
    "HMAC-SHA1",
    "HMAC-MD5",
)

ZONE_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class DnsProviderError(Exception):
    """A provider is misconfigured, or no provider covers a name."""


class DnsProvider(models.Model):
    """
    One set of DNS credentials, and the zones it may write to.

    Zones are explicit rather than discovered. Asking Cloudflare which zones a
    token can edit is possible; asking a BIND server is not, and a model where
    one kind configures itself and the other does not is a model nobody
    remembers how to use.
    """

    class Kind(models.TextChoices):
        CLOUDFLARE = "cloudflare", "Cloudflare"
        RFC2136 = "rfc2136", "BIND, Knot or other RFC2136"

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=20, choices=Kind.choices)

    zones = models.JSONField(
        default=list,
        help_text=(
            "Apex zones this provider is authoritative for, e.g. "
            "['example.com', 'example.net']. A domain is matched to the "
            "provider holding the longest matching zone."
        ),
    )

    # Cloudflare
    api_token = EncryptedTextField(blank=True, default="")

    # RFC2136
    server = models.CharField(max_length=253, blank=True, default="")
    port = models.PositiveIntegerField(default=53)
    tsig_name = models.CharField(max_length=253, blank=True, default="")
    tsig_algorithm = models.CharField(max_length=32, blank=True, default="HMAC-SHA256")
    tsig_secret = EncryptedTextField(blank=True, default="")

    # How long certbot waits after writing the record before asking the CA to
    # check. Too short and validation fails on a zone that has not propagated;
    # too long and every issuance is slow. The defaults differ because the
    # systems differ -- Cloudflare's edge is fast and consistent, a pair of
    # BIND servers with zone transfers is not.
    propagation_seconds = models.PositiveIntegerField(default=60)

    is_enabled = models.BooleanField(default=True)

    last_used_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"

    @property
    def normalised_zones(self) -> list[str]:
        return [str(z).strip().lower().rstrip(".") for z in (self.zones or []) if str(z).strip()]

    def covers(self, name: str) -> str | None:
        """
        The zone of this provider that holds `name`, or None.

        Matching is on label boundaries. A plain suffix check would have
        notexample.com matched by a provider holding example.com, and that
        provider would then be handed a challenge it cannot write.
        """
        candidate = name.lower().rstrip(".")
        if candidate.startswith("*."):
            candidate = candidate[2:]

        best = None
        for zone in self.normalised_zones:
            if candidate == zone or candidate.endswith("." + zone):
                if best is None or len(zone) > len(best):
                    best = zone
        return best

    def validate_config(self) -> None:
        """Everything certbot will need, checked before it is needed."""
        if not self.normalised_zones:
            raise DnsProviderError(
                f"{self.name} has no zones, so no domain can ever be matched to it."
            )
        for zone in self.normalised_zones:
            if not ZONE_RE.match(zone):
                raise DnsProviderError(f"'{zone}' is not a DNS zone name.")

        if self.kind == self.Kind.CLOUDFLARE:
            if not self.api_token:
                raise DnsProviderError(f"{self.name} has no Cloudflare API token.")
        elif self.kind == self.Kind.RFC2136:
            missing = [
                label
                for label, value in (
                    ("nameserver address", self.server),
                    ("TSIG key name", self.tsig_name),
                    ("TSIG secret", self.tsig_secret),
                )
                if not value
            ]
            if missing:
                raise DnsProviderError(f"{self.name} is missing its {', '.join(missing)}.")
            if self.tsig_algorithm not in TSIG_ALGORITHMS:
                raise DnsProviderError(
                    f"'{self.tsig_algorithm}' is not a TSIG algorithm certbot "
                    f"accepts. Use one of: {', '.join(TSIG_ALGORITHMS)}."
                )
        else:
            raise DnsProviderError(f"'{self.kind}' is not a provider kind.")

    # -----------------------------------------------------------------------
    # certbot
    # -----------------------------------------------------------------------

    def credentials_ini(self) -> str:
        """The file certbot's plugin reads, in the format it expects."""
        self.validate_config()

        if self.kind == self.Kind.CLOUDFLARE:
            # Certbot's cloudflare plugin reads a flat key=value file, not a
            # sectioned one, so this is assembled rather than written by
            # configparser.
            return f"dns_cloudflare_api_token = {self.api_token}\n"

        lines = [
            f"dns_rfc2136_server = {self.server}",
            f"dns_rfc2136_port = {self.port}",
            f"dns_rfc2136_name = {self.tsig_name}",
            f"dns_rfc2136_secret = {self.tsig_secret}",
            f"dns_rfc2136_algorithm = {self.tsig_algorithm}",
        ]
        return "\n".join(lines) + "\n"

    def write_credentials(self, directory: Path) -> Path:
        """
        Write the credentials file certbot needs, readable only by this user.

        Certbot refuses a credentials file with loose permissions, which is
        the correct behaviour and worth matching rather than working around:
        this file is a Cloudflare token or a key that can write to a zone.
        """
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, stat.S_IRWXU)

        path = directory / f"dns-{self.kind}-{self.pk or 'new'}.ini"
        # Opened with the mode set at creation, so there is no window in which
        # the file exists and is world-readable.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with io.open(descriptor, "w") as handle:
            handle.write(self.credentials_ini())
        os.chmod(path, 0o600)
        return path

    def certbot_args(self, credentials_path: Path) -> list[str]:
        if self.kind == self.Kind.CLOUDFLARE:
            return [
                "--dns-cloudflare",
                "--dns-cloudflare-credentials", str(credentials_path),
                "--dns-cloudflare-propagation-seconds", str(self.propagation_seconds),
            ]
        return [
            "--dns-rfc2136",
            "--dns-rfc2136-credentials", str(credentials_path),
            "--dns-rfc2136-propagation-seconds", str(self.propagation_seconds),
        ]

    def describe_credentials(self) -> str:
        """For the panel. Enough to tell two apart, not enough to use."""
        if self.kind == self.Kind.CLOUDFLARE:
            if not self.api_token:
                return "no token"
            return f"token ending {self.api_token[-4:]}"
        if not self.server:
            return "not configured"
        return f"{self.server}:{self.port} with key {self.tsig_name}"


def provider_for(name: str) -> DnsProvider | None:
    """
    The provider authoritative for this name, by longest matching zone.

    Longest wins so that a delegated sub-zone on a different system is
    reachable: example.com at Cloudflare and research.example.com on Knot
    coexist, and research.example.com goes to Knot.
    """
    best: tuple[int, DnsProvider] | None = None

    for provider in DnsProvider.objects.filter(is_enabled=True):
        zone = provider.covers(name)
        if zone is None:
            continue
        if best is None or len(zone) > best[0]:
            best = (len(zone), provider)

    return best[1] if best else None
