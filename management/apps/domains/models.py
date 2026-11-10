"""
Domain models.

A Domain represents a hostname (e.g. api.example.com) that the gateway will
accept traffic for. Each domain can have multiple routing rules that send
traffic to different backends.
"""
from django.db import models

from common.validators import validate_config_path, validate_hostname


class Domain(models.Model):
    """
    A hostname served by the gateway.

    Each domain maps to an Nginx `server` block with its own `server_name`.
    """

    name = models.CharField(
        max_length=253,
        unique=True,
        db_index=True,
        help_text="Fully qualified domain name, e.g. api.example.com",
    )
    description = models.TextField(blank=True, default="")

    # SSL
    ssl_enabled = models.BooleanField(default=False)
    ssl_cert_path = models.CharField(max_length=512, blank=True, default="")
    ssl_key_path = models.CharField(max_length=512, blank=True, default="")
    force_ssl_redirect = models.BooleanField(
        default=True,
        help_text="301 redirect HTTP → HTTPS when SSL is enabled",
    )

    acme_challenge_passthrough = models.BooleanField(
        default=False,
        help_text=(
            "Let the backend answer certificate challenges this gateway has no "
            "token for. The gateway still serves its own, so both it and a "
            "backend that manages its own certificates -- Traefik, say -- can "
            "each complete a challenge without taking the path from the other."
        ),
    )

    # State
    is_active = models.BooleanField(default=True, db_index=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def clean(self):
        """
        The name is written straight into `server_name`, where nothing can be
        quoted, so it is constrained here rather than escaped on the way out.
        """
        super().clean()
        validate_hostname(self.name)
        validate_config_path(self.ssl_cert_path, label="certificate path")
        validate_config_path(self.ssl_key_path, label="private key path")

    @property
    def slug(self) -> str:
        """An identifier safe to embed in generated Nginx symbol names."""
        return self.name.replace(".", "_").replace("-", "_")
