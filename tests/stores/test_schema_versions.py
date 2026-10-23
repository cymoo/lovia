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

    # A fresh database: the baseline goes back with the step.
    with pytest.raises(RuntimeError, match="boom"):
        _open(path, broken)
    assert _tables(path) == set()

    # A versioned one: it stays at the version it had, data untouched.
    _open(path, _add_note)
    with pytest.raises(RuntimeError, match="boom"):
        _open(path, _add_note, broken)
    assert _columns(path) == ["id", "note"]
    assert _versions(path) == {"items": 1}
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
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


# A baseline whose index names a column a later step renames, and a table a
# later step drops: replaying the baseline over the migrated database would
# fail on the index and resurrect the table.
_INDEXED_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, old_name TEXT);
CREATE INDEX IF NOT EXISTS idx_items_old ON items(old_name);
CREATE TABLE IF NOT EXISTS scratch (id TEXT);
"""


def _rename(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX idx_items_old")
    conn.execute("ALTER TABLE items RENAME COLUMN old_name TO new_name")


def _drop_scratch(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE scratch")


def _open_indexed(path: Path, *migrations: Migration) -> None:
    store = SQLiteStore(path, _INDEXED_SCHEMA, name="items", migrations=migrations)
    with store._conn():
        pass


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {r[0] for r in rows}
    finally:
        conn.close()


def test_reopen_does_not_replay_the_baseline(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    _open_indexed(path, _rename, _drop_scratch)
    _open_indexed(path, _rename, _drop_scratch)
    assert _columns(path) == ["id", "new_name"]
    assert "scratch" not in _tables(path)


def test_older_lovia_refuses_a_renamed_schema(tmp_path: Path) -> None:
    # The baseline's index names a column that is gone: the refusal must come
    # before any of it runs, not as "no such column".
    path = tmp_path / "x.db"
    _open_indexed(path, _rename, _drop_scratch)
    with pytest.raises(LoviaError, match="schema version 2"):
        _open_indexed(path, _rename)
    assert "scratch" not in _tables(path)


def test_baseline_splits_into_whole_statements(tmp_path: Path) -> None:
    # A trigger body holds semicolons of its own.
    schema = """
CREATE TABLE IF NOT EXISTS items (id TEXT PRIMARY KEY, seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS log (id TEXT);
CREATE TRIGGER IF NOT EXISTS items_log AFTER INSERT ON items BEGIN
    INSERT INTO log (id) VALUES (new.id);
    UPDATE items SET seen = 1 WHERE id = new.id;
END;
"""
    store = SQLiteStore(tmp_path / "x.db", schema, name="items")
    with store._tx() as conn:
        conn.execute("INSERT INTO items (id) VALUES ('a')")
    with store._conn() as conn:
        assert conn.execute("SELECT id FROM log").fetchall()[0][0] == "a"
        assert conn.execute("SELECT seen FROM items").fetchone()[0] == 1


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
