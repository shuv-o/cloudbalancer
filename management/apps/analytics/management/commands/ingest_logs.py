"""
Read the access logs now, rather than waiting for the schedule.

Useful after configuring geolocation, when the rows already written have no
country against them and the next scheduled pass will only cover new traffic.
"""
from django.core.management.base import BaseCommand

from apps.analytics.services import ingest, prune


class Command(BaseCommand):
    help = "Fold new access log lines into the daily rollups."

    def add_arguments(self, parser):
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Apply retention afterwards, deleting addresses past their window.",
        )

    def handle(self, *args, **options):
        result = ingest()

        if result.get("reason"):
            self.stdout.write(self.style.WARNING(result["reason"]))
            return

        self.stdout.write(
            f"Read {result['lines']:,} lines from {result['files']} logs"
            + (f", {result['unparsed']:,} unparsed" if result.get("unparsed") else "")
        )
        self.stdout.write(
            f"  {result.get('clients', 0)} client/day rows, "
            f"{result.get('paths', 0)} path/day rows, "
            f"{result.get('hours', 0)} hour buckets"
        )

        if options["prune"]:
            removed = prune()
            self.stdout.write(
                f"Pruned {removed['clients']} client rows, {removed['paths']} paths, "
                f"{removed['geo']} geo, {removed['hours']} hours"
            )
