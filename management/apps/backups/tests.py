"""
Backups, and the proof that they are backups rather than files.

The tests that matter here are the negative ones. A backup system that writes
archives is easy; one that notices it has been writing unusable archives is
the point, so most of what follows damages an archive on purpose and checks
that something says so.
"""
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.backups import archive
from apps.backups.models import BackupRun
from apps.backups.services import (
    backup_create,
    backup_dir,
    backup_health,
    backup_prune,
    backup_unpack,
    backup_verify,
)
from apps.domains.services import domain_create
from apps.security.roles import Role
from apps.security.testing import make_operator

KEY = "a" * 64


class BackupTestCase(TestCase):
    """
    A fresh backup directory per test.

    Django rolls the database back between tests and leaves the filesystem
    alone, so a shared directory means one test prunes, verifies and counts
    archives that another one wrote. The first version of these tests shared
    it and the failures looked like retention bugs.
    """

    def setUp(self):
        super().setUp()
        directory = tempfile.mkdtemp(prefix="cb-backups-")
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)

        settings_patch = override_settings(BACKUP_DIR=directory)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)


class ArchiveEncryptionTests(TestCase):
    """
    The construction, exercised directly.

    Hand-written framing around an audited cipher is the part of this worth
    testing hardest: AES-GCM will not notice a chunk removed from the end or
    two chunks swapped, and both are what a half-finished copy looks like.
    """

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="cb-arch-"))
        self.key = archive.load_key(KEY)

    def roundtrip(self, payload: bytes) -> bytes:
        plain = self.scratch / "plain"
        sealed = self.scratch / "sealed"
        out = self.scratch / "out"
        plain.write_bytes(payload)
        archive.write(plain, sealed, key=self.key)
        archive.read(sealed, out, key=self.key)
        return out.read_bytes()

    def test_a_small_payload_survives(self):
        self.assertEqual(self.roundtrip(b"hello"), b"hello")

    def test_an_empty_payload_survives(self):
        self.assertEqual(self.roundtrip(b""), b"")

    def test_a_payload_spanning_several_chunks_survives(self):
        payload = os.urandom(archive.CHUNK * 2 + 17)
        self.assertEqual(self.roundtrip(payload), payload)

    def test_a_payload_exactly_one_chunk_survives(self):
        """The boundary where 'is this the last chunk' is decided."""
        payload = os.urandom(archive.CHUNK)
        self.assertEqual(self.roundtrip(payload), payload)

    def test_the_ciphertext_is_not_the_plaintext(self):
        plain = self.scratch / "plain"
        sealed = self.scratch / "sealed"
        plain.write_bytes(b"super secret private key material")
        archive.write(plain, sealed, key=self.key)
        self.assertNotIn(b"private key material", sealed.read_bytes())

    def test_the_wrong_key_is_refused(self):
        plain = self.scratch / "plain"
        sealed = self.scratch / "sealed"
        plain.write_bytes(b"payload")
        archive.write(plain, sealed, key=self.key)

        with self.assertRaises(archive.ArchiveError):
            archive.read(sealed, self.scratch / "out", key=archive.load_key("b" * 64))

    def test_a_flipped_byte_is_refused(self):
        plain = self.scratch / "plain"
        sealed = self.scratch / "sealed"
        plain.write_bytes(os.urandom(2048))
        archive.write(plain, sealed, key=self.key)

        data = bytearray(sealed.read_bytes())
        data[-5] ^= 0xFF
        sealed.write_bytes(bytes(data))

        with self.assertRaises(archive.ArchiveError):
            archive.read(sealed, self.scratch / "out", key=self.key)

    def test_a_truncated_archive_is_refused(self):
        """
        What a copy interrupted halfway looks like.

        Without the end-of-stream flag in the associated data this would
        decrypt happily into a shorter file, and the restore would fail much
        later with something unrelated.
        """
        plain = self.scratch / "plain"
        sealed = self.scratch / "sealed"
        plain.write_bytes(os.urandom(archive.CHUNK * 3))
        archive.write(plain, sealed, key=self.key)

        data = sealed.read_bytes()
        sealed.write_bytes(data[: len(data) // 2])

        with self.assertRaises(archive.ArchiveError):
            archive.read(sealed, self.scratch / "out", key=self.key)

    def test_appended_data_is_refused(self):
        plain = self.scratch / "plain"
        sealed = self.scratch / "sealed"
        plain.write_bytes(b"payload")
        archive.write(plain, sealed, key=self.key)

        with sealed.open("ab") as handle:
            handle.write(b"extra")

        with self.assertRaises(archive.ArchiveError):
            archive.read(sealed, self.scratch / "out", key=self.key)

    def test_an_unencrypted_archive_still_reads_after_a_key_is_added(self):
        """
        Turning encryption on must not orphan yesterday's backups.

        That would make the archives from before the change unreadable at
        precisely the moment someone needs an older one.
        """
        plain = self.scratch / "plain"
        stored = self.scratch / "stored"
        plain.write_bytes(b"older archive")
        archive.write(plain, stored, key=None)

        out = self.scratch / "out"
        archive.read(stored, out, key=self.key)
        self.assertEqual(out.read_bytes(), b"older archive")

    def test_an_encrypted_archive_without_a_key_says_so(self):
        plain = self.scratch / "plain"
        sealed = self.scratch / "sealed"
        plain.write_bytes(b"payload")
        archive.write(plain, sealed, key=self.key)

        with self.assertRaises(archive.ArchiveError) as caught:
            archive.read(sealed, self.scratch / "out", key=None)
        self.assertIn("BACKUP_ENCRYPTION_KEY", str(caught.exception))


class KeyValidationTests(TestCase):
    def test_no_key_means_no_encryption(self):
        self.assertIsNone(archive.load_key(""))
        self.assertIsNone(archive.load_key(None))

    def test_a_key_of_the_wrong_length_is_refused(self):
        """
        Quietly padding or truncating it would produce archives nobody can
        decrypt, discovered during a restore.
        """
        with self.assertRaises(archive.ArchiveError):
            archive.load_key("ab" * 8)

    def test_a_key_that_is_not_hex_is_refused(self):
        with self.assertRaises(archive.ArchiveError):
            archive.load_key("z" * 64)


class BackupCreateTests(BackupTestCase):
    def setUp(self):
        super().setUp()
        domain_create(name="backed-up.example.com")

    def test_a_backup_records_what_it_contains(self):
        run = backup_create(trigger=BackupRun.Trigger.MANUAL)

        self.assertEqual(run.status, BackupRun.Status.COMPLETE, run.error)
        self.assertTrue(run.filename)
        self.assertGreater(run.size_bytes, 0)
        self.assertEqual(len(run.sha256), 64)
        self.assertIn("database", run.components)
        self.assertIn("letsencrypt", run.components)
        self.assertIsNotNone(run.finished_at)

    def test_two_backups_in_the_same_second_do_not_collide(self):
        """
        The nightly job and someone pressing the button, which is exactly
        when a person reaches for it.

        With a filename built from the timestamp alone, the second archive
        overwrote the first and one of the two records then described a file
        that was not there.
        """
        first = backup_create()
        second = backup_create()

        self.assertNotEqual(first.filename, second.filename)
        self.assertTrue((backup_dir() / first.filename).exists())
        self.assertTrue((backup_dir() / second.filename).exists())
        self.assertNotEqual(first.sha256, second.sha256)

    def test_the_archive_is_on_disk_and_only_readable_by_its_owner(self):
        """It contains private keys whether or not encryption is on."""
        run = backup_create()
        path = backup_dir() / run.filename
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_archive_holds_the_four_members(self):
        run = backup_create()
        with tempfile.TemporaryDirectory() as scratch:
            manifest = backup_unpack(run=run, destination=Path(scratch))
            present = {p.name for p in Path(scratch).iterdir()}
            self.assertEqual(
                present,
                {"manifest.json", "database.dump", "letsencrypt.tar", "nginx-conf.tar"},
            )
            self.assertEqual(manifest["version"], 1)

    @override_settings(BACKUP_ENCRYPTION_KEY=KEY)
    def test_an_encrypted_backup_is_marked_and_unreadable_as_a_tar(self):
        run = backup_create()
        self.assertTrue(run.is_encrypted)
        self.assertTrue(run.filename.endswith(".enc"))

        path = backup_dir() / run.filename
        with self.assertRaises(tarfile.ReadError):
            tarfile.open(path, "r:gz")

    @override_settings(BACKUP_ENCRYPTION_KEY=KEY)
    def test_an_encrypted_backup_still_unpacks(self):
        run = backup_create()
        with tempfile.TemporaryDirectory() as scratch:
            manifest = backup_unpack(run=run, destination=Path(scratch))
            self.assertTrue(manifest["encrypted"])

    @override_settings(BACKUP_ENCRYPTION_KEY="not-a-key")
    def test_a_broken_key_fails_the_backup_rather_than_writing_a_plaintext_one(self):
        """
        Falling back to no encryption would be the dangerous recovery.

        Backups would keep appearing in the panel, and every one of them
        would be a plaintext copy of every private key.
        """
        run = backup_create()
        self.assertEqual(run.status, BackupRun.Status.FAILED)
        self.assertIn("BACKUP_ENCRYPTION_KEY", run.error)


class BackupVerifyTests(BackupTestCase):
    def test_a_good_backup_verifies(self):
        run = backup_verify(run=backup_create())
        self.assertEqual(
            run.verification, BackupRun.Verification.PASSED, run.verification_detail
        )
        self.assertIsNotNone(run.verified_at)

    def test_a_missing_archive_fails_verification(self):
        run = backup_create()
        (backup_dir() / run.filename).unlink()

        backup_verify(run=run)
        self.assertEqual(run.verification, BackupRun.Verification.FAILED)
        self.assertIn("no longer on disk", run.verification_detail)

    def test_bit_rot_fails_verification(self):
        """
        The digest is checked before anything is unpacked.

        A backup that silently decays on disk is the failure mode nobody
        watches for, because nothing about it is an error until a restore.
        """
        run = backup_create()
        path = backup_dir() / run.filename
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))

        backup_verify(run=run)
        self.assertEqual(run.verification, BackupRun.Verification.FAILED)
        self.assertIn("digest", run.verification_detail)

    def test_an_archive_missing_a_member_fails_verification(self):
        run = backup_create()
        path = backup_dir() / run.filename

        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            with tarfile.open(path, "r:gz") as tar:
                tar.extractall(work, filter="data")
            (work / "database.dump").unlink()

            rebuilt = work / "rebuilt.tar.gz"
            with tarfile.open(rebuilt, "w:gz") as tar:
                for member in sorted(work.glob("*.json")) + sorted(work.glob("*.tar")):
                    tar.add(member, arcname=member.name)
            path.write_bytes(rebuilt.read_bytes())

        run.sha256 = archive.digest_of(path)
        run.save(update_fields=["sha256"])

        backup_verify(run=run)
        self.assertEqual(run.verification, BackupRun.Verification.FAILED)
        self.assertIn("database.dump", run.verification_detail)

    def test_a_future_manifest_version_fails_rather_than_being_guessed_at(self):
        run = backup_create()
        path = backup_dir() / run.filename

        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            with tarfile.open(path, "r:gz") as tar:
                tar.extractall(work, filter="data")

            manifest = json.loads((work / "manifest.json").read_text())
            manifest["version"] = 99
            (work / "manifest.json").write_text(json.dumps(manifest))

            rebuilt = work / "rebuilt.tar.gz"
            with tarfile.open(rebuilt, "w:gz") as tar:
                for name in ("manifest.json", "database.dump", "letsencrypt.tar",
                             "nginx-conf.tar"):
                    tar.add(work / name, arcname=name)
            path.write_bytes(rebuilt.read_bytes())

        run.sha256 = archive.digest_of(path)
        run.save(update_fields=["sha256"])

        backup_verify(run=run)
        self.assertEqual(run.verification, BackupRun.Verification.FAILED)
        self.assertIn("99", run.verification_detail)


class RetentionTests(BackupTestCase):
    def ageing(self, days: int, *, verified: bool = True) -> BackupRun:
        run = backup_create()
        if verified:
            run.verification = BackupRun.Verification.PASSED
        run.started_at = timezone.now() - timezone.timedelta(days=days)
        run.save()
        return run

    def test_recent_backups_are_kept(self):
        recent = self.ageing(1)
        backup_prune()
        self.assertTrue(BackupRun.objects.filter(pk=recent.pk).exists())

    def test_an_old_backup_and_its_file_both_go(self):
        old = self.ageing(200)
        path = backup_dir() / old.filename

        backup_prune()

        self.assertFalse(BackupRun.objects.filter(pk=old.pk).exists())
        self.assertFalse(path.exists())

    def test_one_backup_a_week_survives_beyond_the_daily_window(self):
        """
        Corruption is often noticed weeks after it was introduced.

        Daily-only retention answers "undo yesterday" and nothing else, by
        which time a fortnight of dailies has rotated past the moment that
        matters.
        """
        for day in (20, 21, 22, 27, 28):
            self.ageing(day)

        backup_prune()

        weeks = {
            run.started_at.strftime("%G-W%V")
            for run in BackupRun.objects.all()
        }
        self.assertGreaterEqual(len(weeks), 2)
        for week in weeks:
            self.assertEqual(
                BackupRun.objects.filter(
                    started_at__week=int(week.split("-W")[1])
                ).count(),
                1,
                f"more than one backup kept for {week}",
            )

    def test_a_file_with_no_record_is_adopted_not_deleted(self):
        """
        The state you are in immediately after restoring the database.

        Restore Tuesday's backup and Wednesday's and Thursday's archives have
        no rows. Deleting unaccounted-for files would destroy the two newest
        backups in existence within hours of a disaster.
        """
        orphan = backup_dir() / "cloudbalancer-20991231T235959Z.tar.gz"
        orphan.write_bytes(b"newer than anything the database knows about")

        result = backup_prune()

        self.assertTrue(orphan.exists())
        self.assertEqual(result["adopted"], 1)
        self.assertTrue(BackupRun.objects.filter(filename=orphan.name).exists())

    def test_an_adopted_backup_then_ages_out_normally(self):
        """Adoption makes it visible; it does not make it permanent."""
        orphan = backup_dir() / "cloudbalancer-20200101T000000Z.tar.gz"
        orphan.write_bytes(b"genuinely old")
        os.utime(orphan, (0, 0))

        backup_prune()
        self.assertEqual(BackupRun.objects.filter(filename=orphan.name).count(), 1)

        backup_prune()
        self.assertFalse(orphan.exists())

    def test_adoption_does_not_run_twice_on_the_same_file(self):
        orphan = backup_dir() / "cloudbalancer-20991231T235959Z.tar.gz"
        orphan.write_bytes(b"x")

        backup_prune()
        second = backup_prune()

        self.assertEqual(second["adopted"], 0)
        self.assertEqual(BackupRun.objects.filter(filename=orphan.name).count(), 1)

    def test_pruning_leaves_unrelated_files_alone(self):
        other = backup_dir() / "notes.txt"
        other.write_text("not ours")
        backup_prune()
        self.assertTrue(other.exists())


class HealthTests(BackupTestCase):
    def test_it_warns_when_nothing_has_been_verified(self):
        backup_create()
        health = backup_health()
        self.assertTrue(
            any("restored into a scratch database" in w for w in health["warnings"])
        )

    def test_it_warns_when_backups_are_not_encrypted(self):
        health = backup_health()
        self.assertTrue(any("not encrypted" in w for w in health["warnings"]))
        self.assertFalse(health["encrypted"])

    @override_settings(BACKUP_ENCRYPTION_KEY=KEY)
    def test_an_encrypted_installation_does_not_get_that_warning(self):
        health = backup_health()
        self.assertFalse(any("not encrypted" in w for w in health["warnings"]))

    def test_it_warns_when_the_newest_verified_backup_is_stale(self):
        run = backup_verify(run=backup_create())
        run.started_at = timezone.now() - timezone.timedelta(days=5)
        run.save()

        health = backup_health()
        self.assertTrue(any("hours old" in w for w in health["warnings"]))

    def test_a_fresh_verified_backup_clears_the_staleness_warning(self):
        backup_verify(run=backup_create())
        health = backup_health()
        self.assertFalse(any("hours old" in w for w in health["warnings"]))
        self.assertIsNotNone(health["newest_verified_at"])


class BackupApiTests(BackupTestCase):
    def setUp(self):
        super().setUp()
        self.admin = make_operator(username="boss", superuser=True)

    def test_an_admin_sees_the_list_and_its_health(self):
        backup_create()
        self.client.force_login(self.admin)

        body = self.client.get("/api/v1/backups/").json()
        self.assertEqual(len(body["backups"]), 1)
        self.assertIn("warnings", body["health"])

    def test_an_operator_cannot_see_backups(self):
        """
        The list names archives holding every private key the gateway serves.

        Their digests and sizes are admin business for the same reason the
        audit log is.
        """
        self.client.force_login(
            make_operator(username="op", superuser=False, role=Role.OPERATOR)
        )
        self.assertEqual(self.client.get("/api/v1/backups/").status_code, 403)

    def test_starting_one_returns_immediately(self):
        self.client.force_login(self.admin)
        response = self.client.post("/api/v1/backups/")
        self.assertEqual(response.status_code, 202)
        self.assertTrue(BackupRun.objects.exists())

    def test_the_last_restorable_backup_cannot_be_deleted(self):
        """
        Otherwise the panel offers a button whose effect is having no backups.
        """
        run = backup_verify(run=backup_create())
        self.client.force_login(self.admin)

        response = self.client.delete(f"/api/v1/backups/{run.pk}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(BackupRun.objects.filter(pk=run.pk).exists())

    def test_one_of_several_can_be_deleted(self):
        keep = backup_verify(run=backup_create())
        spare = backup_verify(run=backup_create())
        self.client.force_login(self.admin)

        self.assertEqual(self.client.delete(f"/api/v1/backups/{spare.pk}/").status_code, 204)
        self.assertTrue(BackupRun.objects.filter(pk=keep.pk).exists())

    def test_an_unverified_backup_can_always_be_deleted(self):
        spare = backup_create()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.delete(f"/api/v1/backups/{spare.pk}/").status_code, 204)

    def test_deleting_removes_the_file_too(self):
        first = backup_verify(run=backup_create())
        spare = backup_create()
        path = backup_dir() / spare.filename
        self.client.force_login(self.admin)

        self.client.delete(f"/api/v1/backups/{spare.pk}/")

        self.assertFalse(path.exists())
        self.assertTrue((backup_dir() / first.filename).exists())
