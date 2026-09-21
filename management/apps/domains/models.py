"""
Domain models.

A Domain represents a hostname (e.g. api.example.com) that the gateway will
accept traffic for. Each domain can have multiple routing rules that send
traffic to different backends.
"""
from django.db import models


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

    # State
    is_active = models.BooleanField(default=True, db_index=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def slug(self) -> str:
        """An identifier safe to embed in generated Nginx symbol names."""
        return self.name.replace(".", "_").replace("-", "_")
