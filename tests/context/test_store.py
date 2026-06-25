"""Unit tests for the pluggable result-store backends."""

from __future__ import annotations

import pytest

from lovia.context import FileResultStore, InMemoryResultStore


# ----------------------------------------------------- InMemoryResultStore ---


async def test_in_memory_put_get_and_overwrite():
    store = InMemoryResultStore()
    assert await store.get("k") is None
    await store.put("k", "v1")
    assert await store.get("k") == "v1"
    await store.put("k", "v2")
    assert await store.get("k") == "v2"


async def test_in_memory_lru_evicts_oldest():
    store = InMemoryResultStore(max_entries=2)
    await store.put("a", "1")
    await store.put("b", "2")
    await store.put("c", "3")  # evicts "a" (least recently used)
    assert await store.get("a") is None
    assert await store.get("b") == "2"
    assert await store.get("c") == "3"


async def test_in_memory_get_refreshes_lru_recency():
    store = InMemoryResultStore(max_entries=2)
    await store.put("a", "1")
    await store.put("b", "2")
    assert await store.get("a") == "1"  # "a" is now most-recently used
    await store.put("c", "3")  # evicts "b", not "a"
    assert await store.get("a") == "1"
    assert await store.get("b") is None


def test_in_memory_rejects_bad_max_entries():
    with pytest.raises(ValueError, match="max_entries"):
        InMemoryResultStore(max_entries=0)


# --------------------------------------------------------- FileResultStore ---


async def test_file_store_round_trip(tmp_path):
    store = FileResultStore(tmp_path / "results")  # dir does not exist yet
    assert await store.get("k") is None
    await store.put("k", "hello\nworld")
    assert await store.get("k") == "hello\nworld"


async def test_file_store_keys_are_injective(tmp_path):
    store = FileResultStore(tmp_path)
    # call_ids with path-unsafe chars must round-trip AND never collide: a lossy
    # sanitizer would map "a/b" and "a:b" to the same file, making recall return
    # the wrong output (recall prefers the store over the transcript).
    await store.put("a/b", "slash")
    await store.put("a:b", "colon")
    assert await store.get("a/b") == "slash"
    assert await store.get("a:b") == "colon"
    # Two distinct keys -> two distinct files, all under the store directory.
    files = [p.name for p in tmp_path.iterdir()]
    assert len(files) == 2
