"""
The two command-line entry points, which had no tests at all.

`make backup` crashed after doing its work, on a KeyError: the service's
return value had been renamed from "orphans" to "adopted" when pruning
started adopting them instead of deleting, and the command still read the old
key. The backup was written and verified; the process then exited non-zero,
which to anyone watching is indistinguishable from a backup that failed.

These commands are the recovery path. They are reached for on the worst day,
by someone who cannot use the panel, and they were the only code here nothing
exercised.
"""
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.backups.models import BackupRun
from apps.backups.services import backup_create, backup_dir, backup_verify


class CommandTestCase(TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.mkdtemp(prefix="cb-cmd-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        patch = override_settings(BACKUP_DIR=directory)
        patch.enable()
        self.addCleanup(patch.disable)

    def run_command(self, *args, **kwargs) -> str:
        out, err = StringIO(), StringIO()
        call_command(*args, stdout=out, stderr=err, **kwargs)
        return out.getvalue() + err.getvalue()


class BackupCommandTests(CommandTestCase):
    def test_it_writes_verifies_and_prunes_without_crashing(self):
        """
        The whole path, end to end. This is the test that was missing: the
        command did all three and then raised on the last line.
        """
        output = self.run_command("backup")

        self.assertIn("Wrote cloudbalancer-", output)
        self.assertIn("Verified", output)
        self.assertIn("Pruned", output)

        run = BackupRun.objects.get()
        self.assertEqual(run.status, BackupRun.Status.COMPLETE)
        self.assertEqual(run.verification, BackupRun.Verification.PASSED)

    def test_it_reads_the_keys_the_service_returns(self):
        """
        The specific defect, pinned. Renaming a key in backup_prune now fails
        here rather than in production.
        """
        import re

        from apps.backups.management.commands import backup as command_module
        from apps.backups.services import backup_prune

        returned = set(backup_prune().keys())

        # Located through the module, not a path relative to the working
        # directory: a check that silently skips when it cannot find its
        # subject is worse than no check, because it reports success.
        source = Path(command_module.__file__)
        self.assertTrue(source.exists(), source)

        read = set(re.findall(r"""result\[['"](\w+)['"]\]""", source.read_text()))
        self.assertTrue(read, "found no result[...] lookups to check")

        missing = read - returned
        self.assertFalse(
            missing,
            f"the command reads {sorted(missing)} from backup_prune(), which "
            f"returns {sorted(returned)}",
        )

    def test_it_says_when_an_archive_was_adopted(self):
        orphan = backup_dir() / "cloudbalancer-20991231T235959Z-aaaaaa.tar.gz"
        orphan.write_bytes(b"found on disk with no record")

        output = self.run_command("backup")

        self.assertIn("Adopted", output)

    def test_it_stays_quiet_when_nothing_was_adopted(self):
        """Noise in a recovery tool is a cost; most runs adopt nothing."""
        self.assertNotIn("Adopted", self.run_command("backup"))

    def test_no_verify_skips_the_scratch_restore(self):
        output = self.run_command("backup", "--no-verify")

        self.assertIn("Wrote", output)
        self.assertNotIn("Verified", output)
        self.assertEqual(
            BackupRun.objects.get().verification, BackupRun.Verification.PENDING
        )

    def test_no_prune_leaves_the_retention_policy_alone(self):
        output = self.run_command("backup", "--no-prune")

        self.assertIn("Wrote", output)
        self.assertNotIn("Pruned", output)

    def test_it_says_the_archive_is_not_encrypted_when_it_is_not(self):
        """
        An archive holds every private key the gateway serves. A run that
        does not say so reads as a run that did.
        """
        self.assertIn("NOT encrypted", self.run_command("backup"))

    @override_settings(BACKUP_ENCRYPTION_KEY="a" * 64)
    def test_it_says_the_archive_is_encrypted_when_it_is(self):
        output = self.run_command("backup")
        self.assertIn("encrypted", output)
        self.assertNotIn("NOT encrypted", output)

    @override_settings(BACKUP_ENCRYPTION_KEY="not-a-valid-key")
    def test_a_failed_backup_exits_non_zero(self):
        """A recovery tool that fails quietly is worse than one that fails."""
        with self.assertRaises(SystemExit) as caught:
            self.run_command("backup")
        self.assertEqual(caught.exception.code, 1)


class RestoreCommandTests(CommandTestCase):
    def test_list_shows_what_exists_and_whether_it_is_verified(self):
        backup_verify(run=backup_create())
        backup_create()

        output = self.run_command("restore", "--list")

        self.assertIn("verified", output)
        self.assertIn("unverified", output)

    def test_listing_an_empty_directory_says_nothing_rather_than_failing(self):
        self.assertEqual(self.run_command("restore", "--list").strip(), "")

    def test_it_refuses_when_no_backup_has_been_verified(self):
        """
        Defaulting to an unverified archive would hand somebody a file
        nothing has ever read back, at the moment they can least afford it.
        """
        backup_create()

        with self.assertRaises(CommandError) as caught:
            self.run_command("restore")

        self.assertIn("No verified backup", str(caught.exception))

    def test_it_unpacks_the_newest_verified_backup(self):
        backup_verify(run=backup_create())
        destination = tempfile.mkdtemp(prefix="cb-restore-")
        self.addCleanup(shutil.rmtree, destination, ignore_errors=True)

        output = self.run_command("restore", "--into", destination)

        self.assertIn("Unpacked", output)
        for member in ("manifest.json", "database.dump", "letsencrypt.tar"):
            self.assertTrue((Path(destination) / member).exists(), member)

    def test_it_prints_the_steps_rather_than_running_them(self):
        """
        Each one is destructive and none is reversible, so they are for a
        person to read. The test pins that they are all there.
        """
        backup_verify(run=backup_create())
        destination = tempfile.mkdtemp(prefix="cb-restore-")
        self.addCleanup(shutil.rmtree, destination, ignore_errors=True)

        output = self.run_command("restore", "--into", destination)

        self.assertIn("docker compose stop", output)
        self.assertIn("pg_restore", output)
        self.assertIn("tar -xf", output)
        self.assertIn("make deploy_config", output)

    def test_an_unknown_archive_names_what_is_available(self):
        backup_verify(run=backup_create())

        with self.assertRaises(CommandError) as caught:
            self.run_command("restore", "nonexistent.tar.gz")

        self.assertIn("cloudbalancer-", str(caught.exception))
