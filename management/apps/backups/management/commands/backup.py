"""
Take a backup from the command line.

Exists alongside the panel because the two are needed at different moments.
The panel is for routine operation; this is for a cron entry outside Docker,
a pre-upgrade snapshot, or a host where the panel is not currently answering
-- which is exactly when someone most wants a copy of the database.
"""
from django.core.management.base import BaseCommand

from apps.backups.models import BackupRun
from apps.backups.services import backup_create, backup_prune, backup_verify


class Command(BaseCommand):
    help = "Take a backup, verify it restores, and apply the retention policy."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-verify",
            action="store_true",
            help="Skip the scratch restore. Faster, and proves nothing.",
        )
        parser.add_argument(
            "--no-prune",
            action="store_true",
            help="Keep every existing backup regardless of the retention policy.",
        )

    def handle(self, *args, **options):
        run = backup_create(trigger=BackupRun.Trigger.COMMAND)

        if run.status != BackupRun.Status.COMPLETE:
            self.stderr.write(self.style.ERROR(f"Backup failed: {run.error}"))
            raise SystemExit(1)

        self.stdout.write(
            f"Wrote {run.filename} ({run.size_bytes:,} bytes, "
            f"{'encrypted' if run.is_encrypted else 'NOT encrypted'})"
        )

        if not options["no_verify"]:
            backup_verify(run=run)
            if run.verification != BackupRun.Verification.PASSED:
                self.stderr.write(
                    self.style.ERROR(f"Verification failed: {run.verification_detail}")
                )
                raise SystemExit(2)
            self.stdout.write(self.style.SUCCESS(f"Verified: {run.verification_detail}"))

        if not options["no_prune"]:
            result = backup_prune()
            self.stdout.write(
                f"Pruned {result['removed']} backups and {result['orphans']} "
                f"orphans, freeing {result['bytes_freed']:,} bytes"
            )
