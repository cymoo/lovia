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


def test_in_memory_is_bounded_by_default():
    # The default must be bounded so a long-lived shared store can't leak.
    assert InMemoryResultStore()._max is not None


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


async def test_file_store_edge_keys_round_trip_without_oserror(tmp_path):
    store = FileResultStore(tmp_path)
    # Long, multibyte, empty, and "_" must all round-trip (no ENAMETOOLONG, no
    # collision between "" and "_") and never raise on get.
    cases = {"x" * 1000: "long", "中" * 200: "cjk", "": "empty", "_": "underscore"}
    for k, v in cases.items():
        await store.put(k, v)
    for k, v in cases.items():
        assert await store.get(k) == v
    # Every distinct key got its own file (injective).
    assert len(list(tmp_path.iterdir())) == len(cases)


async def test_in_memory_unbounded_when_max_entries_is_none():
    store = InMemoryResultStore(max_entries=None)
    for i in range(2_000):
        await store.put(f"k{i}", "v")
    assert await store.get("k0") == "v"  # nothing evicted


async def test_file_store_put_is_atomic_and_leaves_no_temp_files(tmp_path):
    """put writes via temp+rename: an overwrite never exposes a truncated
    file to a concurrent get, and no ``.tmp`` litter survives."""
    store = FileResultStore(tmp_path)
    await store.put("k", "first version")
    await store.put("k", "second version, longer than the first one")
    assert await store.get("k") == "second version, longer than the first one"
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []
    # Exactly one payload file for the key.
    assert len(list(tmp_path.iterdir())) == 1


async def test_file_store_failed_put_cleans_up_its_temp_file(tmp_path):
    store = FileResultStore(tmp_path)
    # Make the rename target un-replaceable: a directory where the file goes.
    store._path("k").mkdir(parents=True)
    with pytest.raises(OSError):
        await store.put("k", "content")
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
