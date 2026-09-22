"""Render the gateway configuration and reload Nginx, from the command line."""
from pathlib import Path

from django.core.management.base import BaseCommand

from apps.gateway.services import (
    config_deploy,
    config_render,
    config_render_combined,
    nginx_test,
)
from apps.routing.models import ConfigDeployLog


class Command(BaseCommand):
    help = "Render the Nginx configuration from the database, verify it, and reload."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the configuration that would be written and stop.",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Only run nginx -t against the configuration already live.",
        )
        parser.add_argument(
            "--render-to",
            metavar="DIR",
            help=(
                "Write the rendered configuration to DIR and stop. Used by CI to "
                "produce files a real Nginx can be asked to verify."
            ),
        )

    def handle(self, *args, **options):
        if options["render_to"]:
            target = Path(options["render_to"])
            target.mkdir(parents=True, exist_ok=True)
            for name, content in config_render().items():
                (target / name).write_text(content, encoding="utf-8")
                self.stdout.write(f"{name} ({len(content)} bytes)")
            return

        if options["dry_run"]:
            self.stdout.write(config_render_combined())
            return

        if options["check"]:
            result = nginx_test()
            style = self.style.SUCCESS if result.success else self.style.ERROR
            self.stdout.write(style(result.output))
            if not result.success:
                raise SystemExit(1)
            return

        log = config_deploy()

        if log.status == ConfigDeployLog.Status.DEPLOYED:
            self.stdout.write(self.style.SUCCESS(f"Deployed. Log #{log.pk}."))
            return

        self.stdout.write(self.style.ERROR(f"Deploy failed. Log #{log.pk}."))
        self.stdout.write(log.error_output)
        raise SystemExit(1)
