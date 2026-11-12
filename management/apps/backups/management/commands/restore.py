"""
Unpack a backup so it can be restored.

This stops short of writing anything live, on purpose. Putting a database
back underneath a running Django means stopping Django first, and a command
that did both would be sawing the branch it sits on -- it would have to kill
the process executing it. So this unpacks, checks what it unpacked, and
prints the three commands that finish the job.

Every one of those commands is destructive and none of them is reversible, so
they are printed for a person to read rather than run on their behalf.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.backups.models import BackupRun
from apps.backups.services import BackupError, backup_dir, backup_unpack


class Command(BaseCommand):
    help = "Unpack a backup archive and print how to restore from it."

    def add_arguments(self, parser):
        parser.add_argument(
            "archive",
            nargs="?",
            help="Archive filename. Omit to use the newest verified backup.",
        )
        parser.add_argument(
            "--into",
            default="/tmp/cloudbalancer-restore",
            help="Directory to unpack into.",
        )
        parser.add_argument("--list", action="store_true", help="List what is available.")

    def handle(self, *args, **options):
        if options["list"]:
            for run in BackupRun.objects.filter(status=BackupRun.Status.COMPLETE)[:20]:
                mark = {
                    BackupRun.Verification.PASSED: "verified",
                    BackupRun.Verification.FAILED: "FAILED VERIFICATION",
                }.get(run.verification, "unverified")
                self.stdout.write(
                    f"{run.started_at:%Y-%m-%d %H:%M}  {run.filename}  "
                    f"{run.size_bytes:>12,}  {mark}"
                )
            return

        if options["archive"]:
            run = BackupRun.objects.filter(filename=options["archive"]).first()
            if run is None:
                raise CommandError(
                    f"No record of {options['archive']}. Files present: "
                    + ", ".join(sorted(p.name for p in backup_dir().glob("cloudbalancer-*")))
                )
        else:
            run = BackupRun.objects.filter(
                status=BackupRun.Status.COMPLETE,
                verification=BackupRun.Verification.PASSED,
            ).first()
            if run is None:
                raise CommandError(
                    "No verified backup exists. Name an archive explicitly if "
                    "you want to restore an unverified one."
                )

        destination = Path(options["into"])
        try:
            manifest = backup_unpack(run=run, destination=destination)
        except BackupError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Unpacked {run.filename} into {destination}"))
        self.stdout.write(f"  taken {manifest['created_at']}")
        for name, detail in manifest["components"].items():
            self.stdout.write(f"  {name:<14} {detail['bytes']:>12,} bytes")

        self.stdout.write("")
        self.stdout.write(self.style.WARNING("To restore. Each step is destructive:"))
        self.stdout.write(f"""
  1. Stop everything that writes:
       docker compose stop django celery celery-beat

  2. Put the database back. --clean drops what is there first, so take a
     backup of the current state before running it if there is any doubt:
       docker compose exec -T postgres pg_restore \\
         --clean --if-exists --no-owner --no-privileges \\
         -U $POSTGRES_USER -d $POSTGRES_DB < {destination}/database.dump

  3. Put the certificates back:
       tar -xf {destination}/letsencrypt.tar -C /etc/letsencrypt

  4. Start up and re-render the configuration from the restored database:
       docker compose start django celery celery-beat
       make deploy_config

  The nginx-conf.tar in that directory is what was running when the backup
  was taken. It is not restored: step 4 regenerates it. Diff the two if you
  want to confirm the restore produced the same gateway.
""")
