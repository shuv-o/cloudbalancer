"""
Prepare a fresh install: an admin account, an ACME account, and a first deploy.

Idempotent, so it is safe to run on every container start.
"""
import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from apps.certificates.models import AcmeAccount
from apps.gateway.services import config_deploy


class Command(BaseCommand):
    help = "Create the first admin user and ACME account, then deploy."

    def add_arguments(self, parser):
        parser.add_argument("--skip-deploy", action="store_true")

    def handle(self, *args, **options):
        username = os.environ.get("ADMIN_USERNAME", "admin")
        password = os.environ.get("ADMIN_PASSWORD", "")
        email = os.environ.get("ADMIN_EMAIL", "admin@example.com")

        if not User.objects.filter(username=username).exists():
            if not password:
                self.stdout.write(
                    self.style.WARNING(
                        "No admin user yet. Set ADMIN_PASSWORD and run this again, "
                        "or use `manage.py createsuperuser`."
                    )
                )
            else:
                User.objects.create_superuser(username, email, password)
                self.stdout.write(self.style.SUCCESS(f"Created admin user '{username}'."))
        else:
            self.stdout.write(f"Admin user '{username}' already exists.")

        acme_email = os.environ.get("ACME_EMAIL", "")
        if acme_email and not AcmeAccount.objects.exists():
            staging = os.environ.get("ACME_STAGING", "false").lower() in ("1", "true", "yes")
            AcmeAccount.objects.create(
                email=acme_email,
                directory_url=(
                    AcmeAccount.Directory.LETSENCRYPT_STAGING
                    if staging
                    else AcmeAccount.Directory.LETSENCRYPT
                ),
                agreed_to_tos=os.environ.get("ACME_AGREE_TOS", "false").lower()
                in ("1", "true", "yes"),
                is_default=True,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Added ACME account {acme_email} "
                    f"({'staging' if staging else 'production'})."
                )
            )

        if options["skip_deploy"]:
            return

        log = config_deploy()
        if log.status == "deployed":
            self.stdout.write(self.style.SUCCESS("Gateway configuration deployed."))
        else:
            self.stdout.write(
                self.style.WARNING(f"First deploy did not complete: {log.error_output}")
            )
