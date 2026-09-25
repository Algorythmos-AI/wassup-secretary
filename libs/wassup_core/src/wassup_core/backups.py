"""Encrypted, verifiable database backup archives — the format shared by the backup job
(ops-worker) and the restore tool (db-admin).

An archive is a gzip'd tar holding ``manifest.json`` and one ``tables/<name>.tsv`` per table
(Postgres ``COPY`` text format), encrypted as a stream of AES-256-GCM frames under a key derived
from the master key with HKDF-SHA256 and a random per-archive salt (so no nonce is ever reused
across archives). Every frame authenticates its position and whether it is the last one: a
truncated, reordered, altered or wrongly-keyed archive is refused as a whole, never restored in
part. Reading an archive verifies each table's byte length, SHA-256 and row count against the
manifest, so "verified" means the stored bytes decrypt to exactly what was dumped.

Nothing here touches a database; it knows files and bytes only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT = "wassup-backup/1"
MAGIC = b"WSBK1\n"
SALT_BYTES = 16
CHUNK_BYTES = 4 * 1024 * 1024
FINAL_FLAG = 0x8000_0000
KEY_INFO = b"wassup-backup-v1"
NAME_RE = re.compile(
    r"^wassup-(?P<env>[a-z0-9-]+)-(?P<stamp>\d{8}T\d{6}Z)-r(?P<rev>[0-9a-f]+)\.wsb$"
)
_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class BackupError(Exception):
    """The archive, key or manifest is not usable. The message never carries data."""


def key_from_hex(value: str) -> bytes:
    value = value.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise BackupError("backup key must be 64 hex characters (32 bytes)")
    return bytes.fromhex(value)


def _derive(key: bytes, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=KEY_INFO).derive(key)


def _aad(salt: bytes, index: int, final: bool) -> bytes:
    return MAGIC + salt + index.to_bytes(8, "big") + bytes([1 if final else 0])


def encrypt_stream(key: bytes, source: BinaryIO, dest: BinaryIO) -> int:
    """Encrypt ``source`` into ``dest``. Returns the bytes written."""
    salt = os.urandom(SALT_BYTES)
    aead = AESGCM(_derive(key, salt))
    dest.write(MAGIC + salt)
    written = len(MAGIC) + SALT_BYTES
    index = 0
    chunk = source.read(CHUNK_BYTES)
    while True:
        following = source.read(CHUNK_BYTES)
        final = not following
        nonce = index.to_bytes(12, "big")
        ciphertext = aead.encrypt(nonce, chunk, _aad(salt, index, final))
        length = len(ciphertext) | (FINAL_FLAG if final else 0)
        dest.write(length.to_bytes(4, "big") + ciphertext)
        written += 4 + len(ciphertext)
        if final:
            return written
        chunk, index = following, index + 1


def _read_exactly(source: BinaryIO, n: int) -> bytes:
    data = source.read(n)
    if len(data) != n:
        raise BackupError("archive is truncated")
    return data


def decrypt_stream(key: bytes, source: BinaryIO, dest: BinaryIO) -> int:
    """Decrypt ``source`` into ``dest``, refusing anything that is not a complete, untouched
    archive made with ``key``. Returns the plaintext bytes written."""
    if source.read(len(MAGIC)) != MAGIC:
        raise BackupError("not a backup archive")
    salt = _read_exactly(source, SALT_BYTES)
    aead = AESGCM(_derive(key, salt))
    written = 0
    index = 0
    while True:
        header = int.from_bytes(_read_exactly(source, 4), "big")
        final = bool(header & FINAL_FLAG)
        ciphertext = _read_exactly(source, header & ~FINAL_FLAG)
        try:
            plain = aead.decrypt(index.to_bytes(12, "big"), ciphertext, _aad(salt, index, final))
        except InvalidTag as exc:
            raise BackupError("archive is corrupt or the key is wrong") from exc
        dest.write(plain)
        written += len(plain)
        if final:
            if source.read(1):
                raise BackupError("archive has trailing data")
            return written
        index += 1


@dataclass(frozen=True)
class TableEntry:
    name: str
    columns: list[str]
    rows: int
    sha256: str
    bytes: int
    # Primary-key columns the rows were ordered by (empty: no key, so only the count is
    # comparable after a restore).
    order_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SequenceEntry:
    name: str
    last_value: int | None


@dataclass(frozen=True)
class Manifest:
    created_at: str
    environment: str
    database: str
    alembic_revision: str
    tables: list[TableEntry]
    sequences: list[SequenceEntry] = field(default_factory=list)
    format: str = FORMAT

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)

    def to_json(self) -> bytes:
        return json.dumps(asdict(self), indent=1, sort_keys=True).encode()

    @classmethod
    def from_json(cls, data: bytes) -> Manifest:
        try:
            raw = json.loads(data)
        except ValueError as exc:
            raise BackupError("manifest is not JSON") from exc
        if not isinstance(raw, dict) or raw.get("format") != FORMAT:
            raise BackupError("manifest has an unknown format")
        try:
            tables = [TableEntry(**t) for t in raw["tables"]]
            sequences = [SequenceEntry(**s) for s in raw.get("sequences", [])]
            manifest = cls(
                created_at=str(raw["created_at"]),
                environment=str(raw["environment"]),
                database=str(raw["database"]),
                alembic_revision=str(raw["alembic_revision"]),
                tables=tables,
                sequences=sequences,
            )
        except (KeyError, TypeError) as exc:
            raise BackupError("manifest is incomplete") from exc
        for table in manifest.tables:
            names_ok = all(_TABLE_RE.match(c) for c in table.columns + table.order_by)
            if not _TABLE_RE.match(table.name) or not names_ok:
                raise BackupError("manifest names an invalid table or column")
        for seq in manifest.sequences:
            if not _TABLE_RE.match(seq.name):
                raise BackupError("manifest names an invalid sequence")
        return manifest


def archive_name(environment: str, revision: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"wassup-{environment}-{stamp}-r{revision}.wsb"


def parse_archive_name(name: str) -> tuple[str, datetime, str] | None:
    """(environment, created_at, revision) for a name this module produced, else None."""
    match = NAME_RE.match(name)
    if not match:
        return None
    created = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    return match["env"], created, match["rev"]


def table_member(name: str) -> str:
    return f"tables/{name}.tsv"


def digest_file(path: Path) -> tuple[str, int, int]:
    """(sha256, bytes, rows) of a COPY text file: one row per newline."""
    sha, size, rows = hashlib.sha256(), 0, 0
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            sha.update(chunk)
            size += len(chunk)
            rows += chunk.count(b"\n")
    return sha.hexdigest(), size, rows


def write_archive(key: bytes, dest: Path, manifest: Manifest, tables_dir: Path) -> int:
    """Pack ``manifest`` and ``tables_dir/<table>.tsv`` into an encrypted archive at ``dest``.
    Every table in the manifest must be present and match it. Returns the archive size."""
    for table in manifest.tables:
        path = tables_dir / f"{table.name}.tsv"
        if not path.is_file():
            raise BackupError(f"missing table file for {table.name}")
        sha, size, rows = digest_file(path)
        if (sha, size, rows) != (table.sha256, table.bytes, table.rows):
            raise BackupError(f"table file for {table.name} does not match the manifest")
    with tempfile.TemporaryFile() as packed:
        with tarfile.open(fileobj=packed, mode="w:gz") as tar:
            data = manifest.to_json()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            for table in manifest.tables:
                tar.add(tables_dir / f"{table.name}.tsv", arcname=table_member(table.name))
        packed.seek(0)
        tmp = dest.with_name(dest.name + ".part")
        with tmp.open("wb") as out:
            size = encrypt_stream(key, packed, out)
        os.replace(tmp, dest)
    return size


def read_archive(key: bytes, source: Path, dest_dir: Path) -> Manifest:
    """Decrypt ``source`` into ``dest_dir`` (``manifest.json`` + ``tables/``) and verify every
    table against the manifest. Raises BackupError on any discrepancy."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as packed:
        with source.open("rb") as inp:
            decrypt_stream(key, inp, packed)
        packed.seek(0)
        try:
            with tarfile.open(fileobj=packed, mode="r:gz") as tar:
                members = tar.getmembers()
                names = {m.name for m in members}
                if "manifest.json" not in names:
                    raise BackupError("archive has no manifest")
                for member in members:
                    if not member.isfile() or not (
                        member.name == "manifest.json"
                        or re.fullmatch(r"tables/[a-z_][a-z0-9_]*\.tsv", member.name)
                    ):
                        raise BackupError("archive contains an unexpected member")
                tar.extractall(dest_dir, filter="data")
        except tarfile.TarError as exc:
            raise BackupError("archive is not a valid tar") from exc
    manifest = Manifest.from_json((dest_dir / "manifest.json").read_bytes())
    for table in manifest.tables:
        path = dest_dir / table_member(table.name)
        if not path.is_file():
            raise BackupError(f"archive is missing table {table.name}")
        sha, size, rows = digest_file(path)
        if (sha, size, rows) != (table.sha256, table.bytes, table.rows):
            raise BackupError(f"table {table.name} does not match the manifest")
    return manifest


def verify_archive(key: bytes, source: Path) -> Manifest:
    """Fully decrypt and check an archive without keeping the plaintext."""
    with tempfile.TemporaryDirectory() as tmp:
        return read_archive(key, source, Path(tmp))


# --- where archives live ------------------------------------------------------------------------


class Store(Protocol):
    @property
    def kind(self) -> str: ...

    def put(self, name: str, path: Path) -> None: ...
    def get(self, name: str, dest: Path) -> None: ...
    def list(self) -> list[str]: ...
    def delete(self, name: str) -> None: ...


@dataclass(frozen=True)
class DirectoryStore:
    """A directory (a mounted volume in the platform, or a folder in a drill)."""

    root: Path
    kind: str = "directory"

    def put(self, name: str, path: Path) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / (name + ".part")
        tmp.write_bytes(path.read_bytes())
        os.replace(tmp, self.root / name)

    def get(self, name: str, dest: Path) -> None:
        try:
            dest.write_bytes((self.root / name).read_bytes())
        except FileNotFoundError as exc:
            raise BackupError(f"no archive named {name}") from exc

    def list(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if parse_archive_name(p.name))

    def delete(self, name: str) -> None:
        (self.root / name).unlink(missing_ok=True)


@dataclass(frozen=True)
class S3Store:
    """An S3-compatible bucket (AWS ap-southeast-2, Cloudflare R2, …). Object lock and
    versioning are bucket settings the owner enables; a delete then only hides a version."""

    bucket: str
    prefix: str
    region: str
    endpoint_url: str | None
    access_key_id: str
    secret_access_key: str
    kind: str = "s3"

    def _client(self) -> Any:
        import boto3  # type: ignore[import-untyped]  # noqa: PLC0415 — only where a bucket is used

        return boto3.client(
            "s3",
            region_name=self.region,
            endpoint_url=self.endpoint_url or None,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
        )

    def _key(self, name: str) -> str:
        return f"{self.prefix.strip('/')}/{name}" if self.prefix.strip("/") else name

    def put(self, name: str, path: Path) -> None:
        self._client().upload_file(str(path), self.bucket, self._key(name))

    def get(self, name: str, dest: Path) -> None:
        self._client().download_file(self.bucket, self._key(name), str(dest))

    def list(self) -> list[str]:
        client = self._client()
        prefix = self._key("")
        names: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for obj in page.get("Contents", []):
                name = str(obj["Key"])[len(prefix) :]
                if parse_archive_name(name):
                    names.append(name)
            token = page.get("NextContinuationToken")
            if not token:
                return sorted(names)

    def delete(self, name: str) -> None:
        self._client().delete_object(Bucket=self.bucket, Key=self._key(name))


def store_from_env(env: Mapping[str, str]) -> Store | None:
    """The configured store, or None. A bucket wins over a directory when both are set.

    WASSUP_BACKUP_S3_BUCKET, WASSUP_BACKUP_S3_ACCESS_KEY_ID, WASSUP_BACKUP_S3_SECRET_ACCESS_KEY
    WASSUP_BACKUP_S3_PREFIX (default wassup-backups), WASSUP_BACKUP_S3_REGION
    (default ap-southeast-2), WASSUP_BACKUP_S3_ENDPOINT_URL (for R2 and the like)
    WASSUP_BACKUP_DIR   a directory instead
    """
    bucket = env.get("WASSUP_BACKUP_S3_BUCKET", "").strip()
    if bucket:
        access, secret = (
            env.get("WASSUP_BACKUP_S3_ACCESS_KEY_ID", ""),
            env.get("WASSUP_BACKUP_S3_SECRET_ACCESS_KEY", ""),
        )
        if not (access and secret):
            raise BackupError("WASSUP_BACKUP_S3_BUCKET needs an access key id and secret")
        return S3Store(
            bucket=bucket,
            prefix=env.get("WASSUP_BACKUP_S3_PREFIX", "wassup-backups"),
            region=env.get("WASSUP_BACKUP_S3_REGION", "ap-southeast-2"),
            endpoint_url=env.get("WASSUP_BACKUP_S3_ENDPOINT_URL", "") or None,
            access_key_id=access,
            secret_access_key=secret,
        )
    directory = env.get("WASSUP_BACKUP_DIR", "").strip()
    if directory:
        return DirectoryStore(Path(directory))
    return None


def prune(store: Store, environment: str, keep: int) -> list[str]:
    """Delete this environment's archives beyond the newest ``keep``. Returns what was deleted."""
    mine = [n for n in store.list() if (p := parse_archive_name(n)) and p[0] == environment]
    doomed = mine[:-keep] if keep > 0 and len(mine) > keep else []
    for name in doomed:
        store.delete(name)
    return doomed
