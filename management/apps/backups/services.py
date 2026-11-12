"""
Taking a backup, proving it works, and putting it back.

Three things are backed up, and they are not equally replaceable:

    the database      every domain, route, backend, certificate record and
                      audit event. Nothing reconstructs it.
    /etc/letsencrypt  the certificates and their private keys. Reissuable,
                      but only at the rate a CA will issue, and only while
                      DNS still points here -- which during a disaster is
                      exactly what is in doubt.
    the Nginx config  reproducible from the database, kept anyway so that a
                      restore can be diffed against what was actually running.

The part most backup systems skip is `backup_verify`. It restores the dump
into a scratch database and counts rows in tables that must not be empty. A
backup that has never been read back is a hypothesis, and the failure that
hurts is not the backup that errors -- that one is visible in the panel the
next morning -- but the one that completes for eight months and turns out to
be unrestorable on the day somebody needs it.
"""
import json
import logging
import os
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.backups.archive import ArchiveError, digest_of, load_key, manifest_of, read, write
from apps.backups.models import BackupRun

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1

# Tables that are empty only on an installation nobody has used. A dump that
# restores but contains none of these restored its schema and lost its data,
# which pg_restore reports as success.
MUST_NOT_BE_EMPTY = ("django_migrations", "auth_user")


class BackupError(Exception):
    """A backup could not be taken, verified, or restored."""


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------

def backup_dir() -> Path:
    directory = Path(settings.BACKUP_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    # The archive holds private keys even when encryption is off.
    os.chmod(directory, 0o700)
    return directory


def _db() -> dict:
    config = settings.DATABASES["default"]
    return {
        "name": config["NAME"],
        "user": config.get("USER") or "",
        "password": config.get("PASSWORD") or "",
        "host": config.get("HOST") or "",
        "port": str(config.get("PORT") or ""),
        "engine": config["ENGINE"],
    }


def _pg_env(db: dict) -> dict:
    env = os.environ.copy()
    if db["password"]:
        # Not on the command line: anything there is visible in `ps` to every
        # other process on the host.
        env["PGPASSWORD"] = db["password"]
    return env


def _run(args: list[str], *, env: dict | None = None, timeout: int | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout or settings.BACKUP_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise BackupError(
            f"{args[0]} is not installed in this container. The management "
            "image needs the PostgreSQL client tools."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"{args[0]} did not finish within {settings.BACKUP_TIMEOUT} seconds."
        ) from exc

    if result.returncode != 0:
        raise BackupError(
            f"{args[0]} failed: {(result.stderr or result.stdout or '').strip()[:500]}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Taking one
# ---------------------------------------------------------------------------

def _dump_database(destination: Path) -> None:
    db = _db()

    if "sqlite" in db["engine"]:
        # Tests and `make dev` run on SQLite. Copying the file is a valid dump
        # of it, and refusing to back up at all would mean the backup path
        # were only ever exercised against Postgres -- which is to say, only
        # in production.
        source = Path(db["name"])
        if source.exists():
            shutil.copy2(source, destination)
        else:
            destination.write_bytes(b"")
        return

    args = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
            "--file", str(destination), "--dbname", db["name"]]
    if db["user"]:
        args += ["--username", db["user"]]
    if db["host"]:
        args += ["--host", db["host"]]
    if db["port"]:
        args += ["--port", db["port"]]

    _run(args, env=_pg_env(db))


def _tar_directory(source: Path, destination: Path) -> int:
    """Tar a directory, following symlinks. Returns the number of files."""
    count = 0
    if not source.exists():
        destination.write_bytes(b"")
        return 0

    with tarfile.open(destination, "w") as tar:
        for path in sorted(source.rglob("*")):
            if path.is_file() or (path.is_symlink() and path.resolve().is_file()):
                # dereference matters: certbot's live/ directory is symlinks
                # into archive/, and a tar of the links alone restores to a
                # set of dangling pointers.
                tar.add(path, arcname=str(path.relative_to(source)))
                count += 1
    return count


def backup_create(*, trigger: str = BackupRun.Trigger.SCHEDULE, actor=None) -> BackupRun:
    """Take one backup. Returns the record, complete or failed."""
    run = BackupRun.objects.create(
        trigger=trigger,
        started_by=actor if getattr(actor, "pk", None) else None,
    )

    # The suffix is not decoration. Two backups in the same second -- the
    # nightly job and someone pressing the button, which is exactly when a
    # person reaches for it -- produced identical names, and the second
    # silently replaced the first. One of the two records then pointed at an
    # archive it did not describe.
    stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
    filename = f"cloudbalancer-{stamp}-{secrets.token_hex(3)}.tar.gz"
    if settings.BACKUP_ENCRYPTION_KEY:
        filename += ".enc"

    try:
        key = load_key(settings.BACKUP_ENCRYPTION_KEY)
        components: dict = {}

        with tempfile.TemporaryDirectory(prefix="cb-backup-") as scratch_name:
            scratch = Path(scratch_name)

            dump = scratch / "database.dump"
            _dump_database(dump)
            components["database"] = {
                "bytes": dump.stat().st_size,
                "sha256": digest_of(dump),
                "engine": _db()["engine"],
            }

            certs = scratch / "letsencrypt.tar"
            cert_count = _tar_directory(Path(settings.LETSENCRYPT_DIR), certs)
            components["letsencrypt"] = {
                "bytes": certs.stat().st_size,
                "sha256": digest_of(certs),
                "files": cert_count,
            }

            conf = scratch / "nginx-conf.tar"
            conf_count = _tar_directory(Path(settings.NGINX_CONF_DIR), conf)
            components["nginx_conf"] = {
                "bytes": conf.stat().st_size,
                "sha256": digest_of(conf),
                "files": conf_count,
            }

            manifest = scratch / "manifest.json"
            manifest.write_text(json.dumps({
                "version": MANIFEST_VERSION,
                "created_at": timezone.now().isoformat(),
                "database": _db()["name"],
                "components": components,
                "encrypted": bool(key),
            }, indent=2))

            plain = scratch / "archive.tar.gz"
            with tarfile.open(plain, "w:gz") as tar:
                for member in (manifest, dump, certs, conf):
                    tar.add(member, arcname=member.name)

            final = backup_dir() / filename
            write(plain, final, key=key)

        run.filename = filename
        run.size_bytes = final.stat().st_size
        run.sha256 = digest_of(final)
        run.is_encrypted = bool(key)
        run.components = components
        run.status = BackupRun.Status.COMPLETE
        run.finished_at = timezone.now()
        run.save()

        logger.info("Backup %s written, %d bytes", filename, run.size_bytes)

    except (BackupError, ArchiveError, OSError) as exc:
        run.status = BackupRun.Status.FAILED
        run.error = str(exc)[:2000]
        run.finished_at = timezone.now()
        run.save()
        logger.error("Backup failed: %s", exc)

    return run


# ---------------------------------------------------------------------------
# Proving one works
# ---------------------------------------------------------------------------

def _verify_postgres_dump(dump: Path) -> str:
    """
    Restore into a scratch database and count rows that must exist.

    A scratch database rather than a schema-only check, because the failures
    worth catching -- a half-written dump, a key that no longer decrypts, a
    pg_dump that silently produced an empty archive -- all parse fine and all
    restore into nothing.
    """
    db = _db()
    scratch = f"cb_verify_{timezone.now().strftime('%Y%m%d%H%M%S')}_{os.getpid()}"

    def psql(target: str, sql: str) -> str:
        args = ["psql", "--no-psqlrc", "--quiet", "--tuples-only", "--no-align",
                "--dbname", target, "--command", sql]
        if db["user"]:
            args += ["--username", db["user"]]
        if db["host"]:
            args += ["--host", db["host"]]
        if db["port"]:
            args += ["--port", db["port"]]
        return _run(args, env=_pg_env(db)).strip()

    psql(db["name"], f'CREATE DATABASE "{scratch}"')
    try:
        args = ["pg_restore", "--no-owner", "--no-privileges", "--dbname", scratch,
                str(dump)]
        if db["user"]:
            args += ["--username", db["user"]]
        if db["host"]:
            args += ["--host", db["host"]]
        if db["port"]:
            args += ["--port", db["port"]]
        _run(args, env=_pg_env(db))

        counts = []
        for table in MUST_NOT_BE_EMPTY:
            rows = psql(scratch, f"SELECT count(*) FROM {table}")
            if not rows.isdigit() or int(rows) == 0:
                raise BackupError(
                    f"The dump restored but {table} is empty, so this archive "
                    "would not bring the panel back."
                )
            counts.append(f"{table}={rows}")
        return "restored into a scratch database; " + ", ".join(counts)
    finally:
        try:
            psql(db["name"], f'DROP DATABASE IF EXISTS "{scratch}"')
        except BackupError:
            logger.warning("Could not drop the scratch database %s", scratch)


def backup_verify(*, run: BackupRun) -> BackupRun:
    """Read an archive back and prove it restores."""
    archive = backup_dir() / run.filename

    try:
        if not run.filename or not archive.exists():
            raise BackupError("The archive is no longer on disk.")

        current = digest_of(archive)
        if run.sha256 and current != run.sha256:
            raise BackupError(
                "The archive on disk no longer matches the digest recorded "
                "when it was written, so it has been altered or corrupted."
            )

        key = load_key(settings.BACKUP_ENCRYPTION_KEY)
        manifest = manifest_of(archive, key=key)
        if manifest.get("version") != MANIFEST_VERSION:
            raise BackupError(
                f"This archive is version {manifest.get('version')}; this "
                f"version of CloudBalancer writes {MANIFEST_VERSION}."
            )

        with tempfile.TemporaryDirectory(prefix="cb-verify-") as scratch_name:
            scratch = Path(scratch_name)
            plain = scratch / "archive.tar.gz"
            read(archive, plain, key=key)

            with tarfile.open(plain, "r:gz") as tar:
                names = tar.getnames()
                for required in ("manifest.json", "database.dump", "letsencrypt.tar"):
                    if required not in names:
                        raise BackupError(f"The archive has no {required}.")
                tar.extract("database.dump", path=scratch, filter="data")

            dump = scratch / "database.dump"
            recorded = manifest["components"]["database"]["sha256"]
            if digest_of(dump) != recorded:
                raise BackupError(
                    "The database dump inside the archive does not match the "
                    "digest taken when it was written."
                )

            if "sqlite" in _db()["engine"]:
                detail = (
                    f"checked the archive and its digests; "
                    f"{dump.stat().st_size} byte dump. A scratch restore needs "
                    "PostgreSQL."
                )
            else:
                detail = _verify_postgres_dump(dump)

        run.verification = BackupRun.Verification.PASSED
        run.verification_detail = detail
    except (BackupError, ArchiveError, OSError, KeyError, ValueError) as exc:
        run.verification = BackupRun.Verification.FAILED
        run.verification_detail = str(exc)[:2000]
        logger.error("Backup %s failed verification: %s", run.filename, exc)

    run.verified_at = timezone.now()
    run.save(update_fields=["verification", "verification_detail", "verified_at"])
    return run


# ---------------------------------------------------------------------------
# Putting one back
# ---------------------------------------------------------------------------

def backup_unpack(*, run: BackupRun, destination: Path) -> dict:
    """
    Unpack an archive to a directory, without touching anything live.

    Restore is deliberately not a button. Putting a database back underneath a
    running Django means stopping it first, and a panel that offered to do
    that from inside itself would be sawing the branch it sits on. This gets
    the contents onto disk and the documented procedure takes it from there.
    """
    archive = backup_dir() / run.filename
    if not archive.exists():
        raise BackupError("The archive is no longer on disk.")

    destination.mkdir(parents=True, exist_ok=True)
    key = load_key(settings.BACKUP_ENCRYPTION_KEY)

    plain = destination / "archive.tar.gz"
    read(archive, plain, key=key)
    with tarfile.open(plain, "r:gz") as tar:
        tar.extractall(path=destination, filter="data")
    plain.unlink()

    manifest = json.loads((destination / "manifest.json").read_text())
    logger.info("Unpacked %s into %s", run.filename, destination)
    return manifest


# ---------------------------------------------------------------------------
# Keeping the disk from filling
# ---------------------------------------------------------------------------

def backup_prune() -> dict:
    """
    Keep recent backups and one per week beyond that.

    Daily-only retention answers "undo yesterday" and nothing else. Corruption
    and bad configuration are often noticed weeks later, by which time a
    fortnight of dailies has rotated past the moment it was introduced.
    """
    keep_days = settings.BACKUP_KEEP_DAYS
    keep_weeks = settings.BACKUP_KEEP_WEEKS
    now = timezone.now()

    keep: set[int] = set()
    weekly: dict[str, BackupRun] = {}

    for run in BackupRun.objects.filter(status=BackupRun.Status.COMPLETE):
        age = now - run.started_at
        if age <= timedelta(days=keep_days):
            keep.add(run.pk)
            continue
        if age <= timedelta(weeks=keep_weeks):
            week = run.started_at.strftime("%G-W%V")
            held = weekly.get(week)
            # The newest in each week: it is the one closest to whatever
            # happened at the end of it.
            if held is None or run.started_at > held.started_at:
                weekly[week] = run

    keep.update(run.pk for run in weekly.values())

    removed, freed = 0, 0
    for run in BackupRun.objects.exclude(pk__in=keep):
        path = backup_dir() / run.filename if run.filename else None
        if path and path.exists():
            freed += path.stat().st_size
            path.unlink()
            removed += 1
        run.delete()

    adopted = backup_adopt_orphans()

    if removed or adopted:
        logger.info("Pruned %d backups and adopted %d orphans, freeing %d bytes",
                    removed, adopted, freed)
    return {"removed": removed, "adopted": adopted, "bytes_freed": freed}


def backup_adopt_orphans() -> int:
    """
    Give a record to every archive on disk that has none.

    Deleting them instead is the obvious implementation and it is dangerous,
    because the commonest way to end up with archives and no rows is to have
    just restored the database. Restore from Tuesday's backup and Wednesday's
    and Thursday's archives are suddenly unaccounted for -- so a prune that
    treats "no row" as "delete" destroys the newest backups in existence
    within hours of a disaster, which is the worst moment it could pick.

    A crash between writing the file and saving the row produces the same
    state, and adopting handles that too: the file becomes visible in the
    panel and ages out under the normal policy rather than living forever.
    """
    known = set(
        BackupRun.objects.exclude(filename="").values_list("filename", flat=True)
    )

    adopted = 0
    for path in sorted(backup_dir().glob("cloudbalancer-*")):
        if path.name in known:
            continue

        stat = path.stat()
        taken = datetime.fromtimestamp(stat.st_mtime, tz=dt_timezone.utc)
        run = BackupRun.objects.create(
            status=BackupRun.Status.COMPLETE,
            trigger=BackupRun.Trigger.COMMAND,
            filename=path.name,
            size_bytes=stat.st_size,
            sha256=digest_of(path),
            is_encrypted=path.suffix == ".enc",
            verification=BackupRun.Verification.PENDING,
            verification_detail=(
                "Found on disk with no record. Adopted rather than deleted: "
                "archives outlive the database that lists them, most often "
                "because the database has just been restored from one."
            ),
            finished_at=taken,
        )
        # auto_now_add ignores what the constructor was given.
        BackupRun.objects.filter(pk=run.pk).update(started_at=taken)
        adopted += 1
        logger.warning("Adopted orphaned backup %s", path.name)

    return adopted


# ---------------------------------------------------------------------------
# Is any of this actually working?
# ---------------------------------------------------------------------------

def backup_health() -> dict:
    """
    One answer to "could we come back from this?", for the dashboard.

    Reports on verified backups, not completed ones. The difference is the
    whole point of the verification pass.
    """
    now = timezone.now()
    latest = BackupRun.objects.filter(status=BackupRun.Status.COMPLETE).first()
    restorable = BackupRun.objects.filter(
        status=BackupRun.Status.COMPLETE,
        verification=BackupRun.Verification.PASSED,
    ).first()

    stale_after = timedelta(hours=settings.BACKUP_STALE_HOURS)
    directory = backup_dir()
    on_disk = list(directory.glob("cloudbalancer-*"))

    warnings = []
    if restorable is None:
        warnings.append(
            "No backup has been restored into a scratch database yet, so "
            "nothing here is known to work."
        )
    elif now - restorable.started_at > stale_after:
        warnings.append(
            f"The newest verified backup is "
            f"{int((now - restorable.started_at).total_seconds() // 3600)} hours old."
        )
    if latest and latest.status == BackupRun.Status.FAILED:
        warnings.append("The most recent backup failed.")
    if not settings.BACKUP_ENCRYPTION_KEY:
        warnings.append(
            "Backups are not encrypted. They contain every TLS private key "
            "this gateway serves, so copying one off this host copies those."
        )

    usage = shutil.disk_usage(directory)
    return {
        "configured": True,
        "directory": str(directory),
        "latest": {
            "id": latest.pk,
            "started_at": latest.started_at,
            "status": latest.status,
            "size_bytes": latest.size_bytes,
            "verification": latest.verification,
        } if latest else None,
        "newest_verified_at": restorable.started_at if restorable else None,
        "total_on_disk": len(on_disk),
        "bytes_on_disk": sum(p.stat().st_size for p in on_disk),
        "disk_free_bytes": usage.free,
        "disk_total_bytes": usage.total,
        "encrypted": bool(settings.BACKUP_ENCRYPTION_KEY),
        "keep_days": settings.BACKUP_KEEP_DAYS,
        "keep_weeks": settings.BACKUP_KEEP_WEEKS,
        "warnings": warnings,
    }
