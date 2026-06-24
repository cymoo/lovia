"""Tests for ChatStore: metadata + title management."""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.transcript import TranscriptEntry, AssistantTextEntry
from lovia.web import ChatStore


async def test_in_memory_roundtrip() -> None:
    store = ChatStore.in_memory()
    assert await store.list() == []
    await store.upsert("s1", agent="bot")
    metas = await store.list()
    assert len(metas) == 1
    assert metas[0].id == "s1"
    assert metas[0].agent == "bot"
    assert metas[0].title is None


async def test_set_title_and_truncate() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    await store.set_title("s1", "  Hello World  " + "x" * 200)
    meta = await store.get("s1")
    assert meta is not None
    assert meta.title is not None
    assert meta.title.startswith("Hello World")
    assert len(meta.title) <= 120


async def test_set_title_if_unchanged_applies_when_provisional_intact() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1", title="Provisional")
    await store.set_title_if_unchanged("s1", "Generated Title", expected="Provisional")
    meta = await store.get("s1")
    assert meta is not None
    assert meta.title == "Generated Title"


async def test_set_title_if_unchanged_skips_after_manual_rename() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1", title="Provisional")
    # User renames before the background-generated title lands.
    await store.set_title("s1", "My Name")
    await store.set_title_if_unchanged("s1", "Generated Title", expected="Provisional")
    meta = await store.get("s1")
    assert meta is not None
    assert meta.title == "My Name"  # not clobbered


async def test_upsert_bumps_updated_at() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1")
    first = await store.get("s1")
    assert first is not None
    await store.upsert("s1")
    second = await store.get("s1")
    assert second is not None
    assert second.updated_at >= first.updated_at
    assert second.created_at == first.created_at


async def test_list_orders_by_updated_at_desc() -> None:
    store = ChatStore.in_memory()
    await store.upsert("old")
    await store.upsert("new")
    await store.upsert("old")  # bump old's updated_at
    ids = [m.id for m in await store.list()]
    assert ids == ["old", "new"]


async def test_delete_removes_transcript_and_meta(tmp_path: Path) -> None:
    store = ChatStore.sqlite(tmp_path / "x.db")
    await store.upsert("s1")
    msg: TranscriptEntry = AssistantTextEntry(content="hi")
    await store.session.append("s1", [msg])
    assert (await store.session.load("s1")) != []
    assert (await store.get("s1")) is not None

    await store.delete("s1")
    assert (await store.session.load("s1")) == []
    assert (await store.get("s1")) is None


async def test_sqlite_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    s1 = ChatStore.sqlite(path)
    await s1.upsert("s1", agent="bot")
    await s1.set_title("s1", "First chat")

    s2 = ChatStore.sqlite(path)
    meta = await s2.get("s1")
    assert meta is not None
    assert meta.title == "First chat"
    assert meta.agent == "bot"


async def test_get_missing_returns_none() -> None:
    store = ChatStore.in_memory()
    assert (await store.get("nope")) is None


@pytest.mark.parametrize("agent", [None, "alpha"])
async def test_upsert_with_optional_agent(agent: str | None) -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1", agent=agent)
    meta = await store.get("s1")
    assert meta is not None
    assert meta.agent == agent
