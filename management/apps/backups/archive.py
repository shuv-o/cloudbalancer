"""
Reading and writing a backup archive, with optional encryption.

An archive is a tar.gz holding four things:

    manifest.json     what this archive is, and the digest of every member
    database.dump     pg_dump custom format, which pg_restore reads
    letsencrypt.tar   certificates and their private keys
    nginx-conf.tar    the generated configuration at the time of the backup

The configuration is reproducible from the database, so strictly it does not
need backing up. It is here because a restore is performed by someone having a
bad day, and being able to diff what was running against what the control
plane would now generate has settled more than one argument about whether a
restore actually worked.

Encryption is optional and off by default. An archive sitting on the same host
as the data it protects gains little from a key that also sits on that host.
It matters the moment the archive is copied anywhere else, which is the normal
next step and the reason this exists: the archive contains every TLS private
key the gateway serves.

The construction is AES-256-GCM over fixed-size chunks. Each chunk gets a
nonce of a random 4-byte prefix and an 8-byte counter, and the chunk's index
and whether it is the last chunk are authenticated as associated data. That
last part is what makes truncating or reordering an archive a decryption
failure rather than a shorter archive -- GCM alone would not notice either.
"""
import hashlib
import json
import os
import struct
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"CBBK1\0"
CHUNK = 1024 * 1024
NONCE_PREFIX = 4
KEY_BYTES = 32


class ArchiveError(Exception):
    """An archive could not be written, read, or trusted."""


def digest_of(path: Path) -> str:
    """SHA-256 of a file, read in chunks so a large dump does not load at once."""
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()


def load_key(raw: str | None) -> bytes | None:
    """
    Turn the configured key into bytes, or None when encryption is off.

    Refuses anything that is not exactly 32 bytes of hex. A key of the wrong
    length is a configuration mistake, and the one thing that must not happen
    is for it to be quietly padded, truncated, or ignored -- that produces
    archives nobody can decrypt, discovered during a restore.
    """
    if not raw:
        return None

    cleaned = raw.strip()
    try:
        key = bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ArchiveError(
            "BACKUP_ENCRYPTION_KEY must be 64 hex characters. Generate one with "
            "`openssl rand -hex 32`."
        ) from exc

    if len(key) != KEY_BYTES:
        raise ArchiveError(
            f"BACKUP_ENCRYPTION_KEY decodes to {len(key)} bytes; it must be "
            f"{KEY_BYTES}. Generate one with `openssl rand -hex 32`."
        )
    return key


def _seal(plain: BinaryIO, out: BinaryIO, key: bytes) -> None:
    aes = AESGCM(key)
    prefix = os.urandom(NONCE_PREFIX)
    out.write(MAGIC)
    out.write(prefix)

    index = 0
    chunk = plain.read(CHUNK)
    while True:
        following = plain.read(CHUNK)
        last = not following
        nonce = prefix + struct.pack(">Q", index)
        # The index and the end-of-stream flag are authenticated but not
        # stored: a reordered or truncated archive fails to decrypt rather
        # than decrypting to something shorter.
        aad = struct.pack(">Q?", index, last)
        sealed = aes.encrypt(nonce, chunk, aad)
        out.write(struct.pack(">I", len(sealed)))
        out.write(sealed)

        if last:
            break
        chunk = following
        index += 1


def _open_sealed(sealed_file: BinaryIO, out: BinaryIO, key: bytes) -> None:
    aes = AESGCM(key)

    if sealed_file.read(len(MAGIC)) != MAGIC:
        raise ArchiveError(
            "This file is not an encrypted CloudBalancer archive. If it was "
            "written without a key, restore it without one."
        )
    prefix = sealed_file.read(NONCE_PREFIX)
    if len(prefix) != NONCE_PREFIX:
        raise ArchiveError("The archive ends before its header does.")

    index = 0
    while True:
        header = sealed_file.read(4)
        if not header:
            raise ArchiveError(
                "The archive ends without a final chunk, so it was truncated "
                "in transit or while being written."
            )
        (length,) = struct.unpack(">I", header)
        body = sealed_file.read(length)
        if len(body) != length:
            raise ArchiveError("The archive ends mid-chunk; it is incomplete.")

        nonce = prefix + struct.pack(">Q", index)
        for last in (False, True):
            try:
                out.write(aes.decrypt(nonce, body, struct.pack(">Q?", index, last)))
                break
            except InvalidTag:
                if last:
                    raise ArchiveError(
                        "The archive does not decrypt with this key, or it has "
                        "been altered since it was written."
                    ) from None
        else:  # pragma: no cover - the loop always breaks or raises
            raise ArchiveError("Unreachable")

        if last:
            if sealed_file.read(1):
                raise ArchiveError(
                    "There is data after the final chunk, so this archive has "
                    "been appended to."
                )
            return
        index += 1


def write(source: Path, destination: Path, *, key: bytes | None) -> None:
    """Copy `source` to `destination`, encrypting it when a key is given."""
    if key is None:
        source.replace(destination)
        destination.chmod(0o600)
        return

    with source.open("rb") as plain, destination.open("wb") as out:
        _seal(plain, out, key)
    destination.chmod(0o600)
    source.unlink()


def read(source: Path, destination: Path, *, key: bytes | None) -> None:
    """
    Copy `destination` out of `source`, decrypting when a key is given.

    Reads the magic rather than trusting the caller: being handed the wrong
    answer about whether an archive is encrypted is a restore that fails
    confusingly, which is the worst moment for it.
    """
    with source.open("rb") as handle:
        encrypted = handle.read(len(MAGIC)) == MAGIC

    if encrypted and key is None:
        raise ArchiveError(
            "This archive is encrypted and no key is configured. Set "
            "BACKUP_ENCRYPTION_KEY to the key it was written with."
        )
    if not encrypted:
        if key is not None:
            # Not an error. A key added later must not make older archives
            # unreadable, which is exactly when they are most needed.
            pass
        destination.write_bytes(source.read_bytes())
        return

    with source.open("rb") as sealed, destination.open("wb") as out:
        _open_sealed(sealed, out, key)


def manifest_of(archive: Path, *, key: bytes | None) -> dict:
    """Read just the manifest out of an archive, without unpacking it."""
    import tarfile
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        plain = Path(scratch) / "archive.tar.gz"
        read(archive, plain, key=key)
        with tarfile.open(plain, "r:gz") as tar:
            try:
                member = tar.extractfile("manifest.json")
            except KeyError:
                raise ArchiveError("This archive has no manifest.") from None
            if member is None:
                raise ArchiveError("This archive's manifest is not a file.")
            return json.loads(member.read().decode())
