"""Versioned SQLite schemas: ``SQLiteStore(name=, migrations=)``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lovia.exceptions import LoviaError
from lovia.stores import SQLiteCheckpointer, SQLiteSession
from lovia.stores._sqlite import Migration, SQLiteStore

_SCHEMA = "CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY);"


def _add_note(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE items ADD COLUMN note TEXT")


def _add_rank(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE items ADD COLUMN rank INTEGER NOT NULL DEFAULT 0")


def _open(path: Path, *migrations: Migration, name: str = "items") -> SQLiteStore:
    store = SQLiteStore(path, _SCHEMA, name=name, migrations=migrations)
    with store._conn():
        pass
    return store


def _versions(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return dict(conn.execute("SELECT store, version FROM lovia_schema"))
    finally:
        conn.close()


def _columns(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(items)")]
    finally:
        conn.close()


def test_fresh_database_runs_every_migration(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    _open(path, _add_note, _add_rank)
    assert _columns(path) == ["id", "note", "rank"]
    assert _versions(path) == {"items": 2}


def test_reopen_applies_only_new_migrations(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    _open(path, _add_note)
    # Re-running ``_add_note`` would fail on the duplicate column.
    _open(path, _add_note, _add_rank)
    _open(path, _add_note, _add_rank)
    assert _columns(path) == ["id", "note", "rank"]
    assert _versions(path) == {"items": 2}


def test_no_migrations_records_nothing(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    _open(path)
    assert _versions(path) == {}


def test_newer_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    _open(path, _add_note, _add_rank)
    with pytest.raises(LoviaError, match="schema version 2") as info:
        _open(path, _add_note)
    assert "upgrade lovia" in str(info.value)


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "x.db"

    def broken(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO items (id) VALUES ('half-done')")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        _open(path, _add_note, broken)
    assert _columns(path) == ["id"]
    assert _versions(path) == {}
    # The next open retries from where the database really is.
    _open(path, _add_note, _add_rank)
    assert _versions(path) == {"items": 2}


def test_stores_sharing_a_file_keep_their_own_version(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    _open(path, _add_note, _add_rank, name="items")
    _open(path, name="other")
    SQLiteSession(path)._connect().close()
    SQLiteCheckpointer(path)._connect().close()
    assert _versions(path) == {"items": 2}


def test_in_memory_store_migrates_at_construction() -> None:
    store = SQLiteStore(
        ":memory:", _SCHEMA, name="items", migrations=(_add_note, _add_rank)
    )
    with store._conn() as conn:
        assert [r[1] for r in conn.execute("PRAGMA table_info(items)")] == [
            "id",
            "note",
            "rank",
        ]
