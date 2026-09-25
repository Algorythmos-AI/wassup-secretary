"""The archive format: encrypted, framed, verified; tampering of any kind is refused."""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wassup_core import backups
from wassup_core.backups import (
    BackupError,
    DirectoryStore,
    Manifest,
    TableEntry,
    archive_name,
    decrypt_stream,
    digest_file,
    encrypt_stream,
    key_from_hex,
    parse_archive_name,
    prune,
    read_archive,
    verify_archive,
    write_archive,
)

KEY = key_from_hex("00" * 31 + "01")
OTHER_KEY = key_from_hex("ff" * 32)


def _roundtrip(data: bytes, key: bytes = KEY) -> bytes:
    sealed = io.BytesIO()
    encrypt_stream(key, io.BytesIO(data), sealed)
    opened = io.BytesIO()
    decrypt_stream(key, io.BytesIO(sealed.getvalue()), opened)
    return opened.getvalue()


def test_key_must_be_32_bytes_of_hex() -> None:
    for bad in ("", "abc", "zz" * 32, "00" * 31):
        with pytest.raises(BackupError):
            key_from_hex(bad)
    assert len(key_from_hex(" " + "ab" * 32 + "\n")) == 32


@pytest.mark.parametrize("size", [0, 1, 100, backups.CHUNK_BYTES, backups.CHUNK_BYTES + 1])
def test_stream_round_trips_any_size(size: int) -> None:
    data = os.urandom(size)
    assert _roundtrip(data) == data


def test_every_kind_of_tampering_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backups, "CHUNK_BYTES", 1000)  # several frames from little data
    data = os.urandom(3500)
    sealed = io.BytesIO()
    encrypt_stream(KEY, io.BytesIO(data), sealed)
    good = sealed.getvalue()
    header = len(backups.MAGIC) + backups.SALT_BYTES

    def refuse(blob: bytes, key: bytes = KEY) -> str:
        with pytest.raises(BackupError) as caught:
            decrypt_stream(key, io.BytesIO(blob), io.BytesIO())
        return str(caught.value)

    assert "wrong" in refuse(good, OTHER_KEY)
    flipped = bytearray(good)
    flipped[header + 40] ^= 0x01
    assert "corrupt" in refuse(bytes(flipped))
    assert "truncated" in refuse(good[: len(good) // 2])
    assert "truncated" in refuse(good[:-1])
    assert "trailing" in refuse(good + b"\x00")
    assert "not a backup" in refuse(b"nope" + good[4:])
    # Frames re-ordered: each frame authenticates its index, so the swap is caught.
    frame_len = 4 + 1000 + 16
    first = good[header : header + frame_len]
    second = good[header + frame_len : header + 2 * frame_len]
    swapped = good[:header] + second + first + good[header + 2 * frame_len :]
    assert "corrupt" in refuse(swapped)
    # A frame appended after the final one: refused (the final flag is authenticated).
    assert "trailing" in refuse(good + first)
    # The final flag cleared on the last frame (so the reader expects more): refused.
    last_header = len(good) - (4 + 500 + 16)
    cleared = bytearray(good)
    cleared[last_header] &= 0x7F
    assert "corrupt" in refuse(bytes(cleared) + first)


def _manifest(tmp_path: Path, rows: bytes = b"1\tx\n2\ty\n") -> tuple[Manifest, Path]:
    tables = tmp_path / "tables"
    tables.mkdir(exist_ok=True)
    (tables / "things.tsv").write_bytes(rows)
    sha, size, count = digest_file(tables / "things.tsv")
    manifest = Manifest(
        created_at=datetime.now(UTC).isoformat(),
        environment="test",
        database="db",
        alembic_revision="0013",
        tables=[
            TableEntry(
                name="things",
                columns=["id", "v"],
                rows=count,
                sha256=sha,
                bytes=size,
                order_by=["id"],
            )
        ],
    )
    return manifest, tables


def test_archive_round_trip_verifies_every_table(tmp_path: Path) -> None:
    manifest, tables = _manifest(tmp_path)
    archive = tmp_path / archive_name("test", "0013")
    size = write_archive(KEY, archive, manifest, tables)
    assert archive.stat().st_size == size and not archive.with_name(archive.name + ".part").exists()
    assert verify_archive(KEY, archive) == manifest
    out = tmp_path / "out"
    assert read_archive(KEY, archive, out) == manifest
    assert (out / "tables" / "things.tsv").read_bytes() == b"1\tx\n2\ty\n"
    with pytest.raises(BackupError):
        verify_archive(OTHER_KEY, archive)


def test_manifest_and_table_must_agree(tmp_path: Path) -> None:
    manifest, tables = _manifest(tmp_path)
    (tables / "things.tsv").write_bytes(b"1\tx\n2\ty\n3\tz\n")  # one row more than declared
    with pytest.raises(BackupError, match="does not match"):
        write_archive(KEY, tmp_path / "a.wsb", manifest, tables)
    with pytest.raises(BackupError, match="missing"):
        write_archive(KEY, tmp_path / "a.wsb", manifest, tmp_path / "elsewhere")


def test_manifest_json_is_validated() -> None:
    with pytest.raises(BackupError):
        Manifest.from_json(b"not json")
    with pytest.raises(BackupError, match="format"):
        Manifest.from_json(b'{"format": "other"}')
    with pytest.raises(BackupError, match="incomplete"):
        Manifest.from_json(b'{"format": "wassup-backup/1", "tables": []}')
    bad_name = (
        b'{"format": "wassup-backup/1", "created_at": "t", "environment": "e", "database": "d", '
        b'"alembic_revision": "1", "tables": [{"name": "x; drop", "columns": [], "rows": 0, '
        b'"sha256": "", "bytes": 0}]}'
    )
    with pytest.raises(BackupError, match="invalid table"):
        Manifest.from_json(bad_name)


def test_archive_names_sort_by_time_and_carry_their_environment() -> None:
    a = archive_name("staging", "0013", datetime(2026, 9, 25, 3, 30, tzinfo=UTC))
    b = archive_name("staging", "0013", datetime(2026, 9, 26, 3, 30, tzinfo=UTC))
    assert a < b
    assert parse_archive_name(a) == ("staging", datetime(2026, 9, 25, 3, 30, tzinfo=UTC), "0013")
    assert parse_archive_name("random.txt") is None


def test_directory_store_and_prune_keep_the_newest_per_environment(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "store")
    src = tmp_path / "src.bin"
    src.write_bytes(b"data")
    staging = [
        archive_name("staging", "0013", datetime(2026, 9, d, 3, 30, tzinfo=UTC))
        for d in range(1, 6)
    ]
    production = archive_name("production", "0013", datetime(2026, 9, 1, 3, 30, tzinfo=UTC))
    for name in [*staging, production]:
        store.put(name, src)
    (tmp_path / "store" / "stray.txt").write_text("ignored")
    assert store.list() == sorted([*staging, production])
    assert prune(store, "staging", keep=2) == staging[:3]  # the 3 oldest staging ones
    assert len(store.list()) == 3 and any(n.startswith("wassup-production") for n in store.list())
    fetched = tmp_path / "fetched"
    store.get(store.list()[-1], fetched)
    assert fetched.read_bytes() == b"data"
    with pytest.raises(BackupError):
        store.get("wassup-staging-20200101T000000Z-r0.wsb", fetched)
