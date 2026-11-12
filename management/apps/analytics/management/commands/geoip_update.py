"""
Fetch the geolocation databases now.

Run once after deploying, and then it looks after itself on the 4th of each
month. Without it every lookup returns empty and the analytics work without
geography rather than failing.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.analytics.geoip_download import update_all


class Command(BaseCommand):
    help = "Download or refresh the IP geolocation databases."

    def handle(self, *args, **options):
        self.stdout.write(f"Provider: {settings.GEOIP_PROVIDER}")
        if settings.GEOIP_PROVIDER.lower() == "dbip":
            self.stdout.write(f"  {settings.GEOIP_ATTRIBUTION}")
        self.stdout.write("")

        failed = False
        for result in update_all():
            if "error" in result:
                failed = True
                self.stderr.write(self.style.ERROR(f"{result['kind']}: {result['error']}"))
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{result['kind']}: {result['bytes']:,} bytes -> {result['path']}"
                    )
                )

        if failed:
            self.stdout.write("")
            self.stdout.write(
                "A missing database is not fatal: lookups return empty and "
                "everything else in the analytics still works."
            )
            raise SystemExit(1)
