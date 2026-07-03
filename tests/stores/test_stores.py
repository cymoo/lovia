from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
)
from lovia.stores import InMemorySession, SQLiteSession


async def test_in_memory_session() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="hi")])
    entries = await s.load("u1")
    assert len(entries) == 1
    assert isinstance(entries[0], InputEntry)
    assert entries[0].content == "hi"
    await s.clear("u1")
    assert await s.load("u1") == []


async def test_sqlite_session_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.db"
        s = SQLiteSession(path)
        await s.append(
            "u1",
            [
                InputEntry(role="user", content="hi"),
                AssistantTextEntry(content="hello"),
            ],
        )
        entries = await s.load("u1")
        assert [type(it).__name__ for it in entries] == [
            "InputEntry",
            "AssistantTextEntry",
        ]
        assert entries[0].content == "hi"  # type: ignore[union-attr]
        assert entries[1].content == "hello"  # type: ignore[union-attr]


# ------------------------------------------------------- InMemorySession ---


async def test_in_memory_append_accumulates_segments() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="one")])
    await s.append("u1", [AssistantTextEntry(content="two")])
    entries = await s.load("u1")
    # Append-only: each run is a segment; load concatenates them in order.
    assert [e.content for e in entries] == ["one", "two"]  # type: ignore[union-attr]


async def test_in_memory_sessions_are_isolated() -> None:
    s = InMemorySession()
    await s.append("a", [InputEntry(role="user", content="for-a")])
    await s.append("b", [InputEntry(role="user", content="for-b")])
    assert (await s.load("a"))[0].content == "for-a"  # type: ignore[union-attr]
    assert (await s.load("b"))[0].content == "for-b"  # type: ignore[union-attr]
    await s.clear("a")
    assert await s.load("a") == []
    assert len(await s.load("b")) == 1  # clearing one leaves the other


async def test_in_memory_load_returns_a_copy() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="hi")])
    got = await s.load("u1")
    got.append(InputEntry(role="user", content="injected"))
    # Mutating the returned list must not leak back into the store.
    assert len(await s.load("u1")) == 1


async def test_in_memory_append_copies_input_list() -> None:
    s = InMemorySession()
    src = [InputEntry(role="user", content="hi")]
    await s.append("u1", src)
    src.append(InputEntry(role="user", content="late"))
    # Mutating the caller's list after append must not affect the store.
    assert len(await s.load("u1")) == 1


# --------------------------------------------------------- SQLiteSession ---


async def test_sqlite_append_segments_and_clear() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        await s.append("u1", [InputEntry(role="user", content="old")])
        await s.append("u1", [InputEntry(role="user", content="new")])
        entries = await s.load("u1")
        assert [e.content for e in entries] == ["old", "new"]  # type: ignore[union-attr]
        await s.clear("u1")
        assert await s.load("u1") == []


async def test_sqlite_append_persists_run_id_and_meta() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        rid = await s.append(
            "u1", [InputEntry(role="user", content="x")], run_id="r1", meta={"k": "v"}
        )
        assert rid == "r1"
        # run_id is a first-class column; meta is opaque, persisted verbatim.
        conn = s._connect()
        try:
            row = conn.execute(
                "SELECT run_id, meta_json FROM session_runs WHERE session_id = ?",
                ("u1",),
            ).fetchone()
        finally:
            s._release(conn)
        assert row[0] == "r1"
        assert json.loads(row[1]) == {"k": "v"}


async def test_sqlite_append_is_idempotent_per_run_id() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        await s.append("u1", [InputEntry(role="user", content="a")], run_id="r1")
        # Re-appending the same run_id is a no-op (first write wins).
        again = await s.append(
            "u1", [InputEntry(role="user", content="DUP")], run_id="r1"
        )
        assert again == "r1"
        assert [e.content for e in await s.load("u1")] == ["a"]  # type: ignore[union-attr]


async def test_append_generates_run_id_when_absent() -> None:
    s = InMemorySession()
    r1 = await s.append("u1", [InputEntry(role="user", content="one")])
    r2 = await s.append("u1", [InputEntry(role="user", content="two")])
    # Each omitted run_id gets a distinct generated id; both segments persist.
    assert r1 and r2 and r1 != r2
    assert [e.content for e in await s.load("u1")] == ["one", "two"]  # type: ignore[union-attr]


async def test_sqlite_sessions_are_isolated_and_ordered() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        s = SQLiteSession(Path(tmp) / "s.db")
        await s.append("a", [InputEntry(role="user", content="a1")])
        await s.append("b", [InputEntry(role="user", content="b1")])
        await s.append("a", [AssistantTextEntry(content="a2")])
        # Order preserved across separate appends; sessions don't bleed.
        assert [e.content for e in await s.load("a")] == ["a1", "a2"]  # type: ignore[union-attr]
        assert [e.content for e in await s.load("b")] == ["b1"]  # type: ignore[union-attr]


async def test_sqlite_memory_path_shares_one_connection() -> None:
    # ":memory:" must hold a single connection open, otherwise each call
    # would see a brand-new empty database.
    s = SQLiteSession(":memory:")
    await s.append("u1", [InputEntry(role="user", content="persisted")])
    entries = await s.load("u1")
    assert [e.content for e in entries] == ["persisted"]  # type: ignore[union-attr]


# ----------------------------------------------------- segments() primitive ---


@pytest.mark.parametrize(
    "make", [lambda: InMemorySession(), lambda: SQLiteSession(":memory:")]
)
async def test_segments_round_trip_run_id_and_meta(make) -> None:
    s = make()
    await s.append(
        "u1", [InputEntry(role="user", content="one")], run_id="r1", meta={"m": 1}
    )
    await s.append("u1", [InputEntry(role="user", content="two")], run_id="r2")

    segs = await s.segments("u1")
    assert [seg.run_id for seg in segs] == ["r1", "r2"]
    assert [seg.meta for seg in segs] == [{"m": 1}, None]
    assert [e.content for seg in segs for e in seg.entries] == ["one", "two"]  # type: ignore[union-attr]

    # load() (inherited default) flattens segments identically.
    assert [e.content for e in await s.load("u1")] == ["one", "two"]  # type: ignore[union-attr]


async def test_in_memory_segments_returns_a_copy() -> None:
    s = InMemorySession()
    await s.append("u1", [InputEntry(role="user", content="x")])
    segs = await s.segments("u1")
    segs[0].entries.append(InputEntry(role="user", content="leak"))
    # Mutating the returned segment must not corrupt stored state.
    assert len(await s.load("u1")) == 1


async def test_in_memory_meta_isolated_on_write_and_read() -> None:
    s = InMemorySession()
    meta = {"carry": {"cleared": ["a"]}}
    await s.append("u1", [InputEntry(role="user", content="x")], run_id="r1", meta=meta)
    # (1) Mutating the caller's dict after append must not change stored state.
    meta["carry"]["cleared"].append("LEAK")
    assert (await s.segments("u1"))[0].meta == {"carry": {"cleared": ["a"]}}
    # (2) Mutating the meta returned by segments() must not corrupt the store.
    segs = await s.segments("u1")
    segs[0].meta["carry"]["cleared"].append("LEAK2")
    assert (await s.segments("u1"))[0].meta == {"carry": {"cleared": ["a"]}}


# -------------------------------------------------------- trim_tool_results ---


def _run_with_result(i: int, chars: int = 5_000) -> list:
    return [
        InputEntry(role="user", content=f"q{i}"),
        ToolCallEntry(call_id=f"c{i}", name="f", arguments="{}"),
        ToolResultEntry(call_id=f"c{i}", output="r" * chars, raw={"big": True}),
        AssistantTextEntry(content=f"a{i}"),
    ]


@pytest.fixture(params=["memory", "sqlite"])
def trim_session(request, tmp_path):
    if request.param == "memory":
        return InMemorySession()
    return SQLiteSession(tmp_path / "trim.db")


async def test_trim_truncates_old_runs_and_keeps_recent(trim_session) -> None:
    s = trim_session
    for i in range(3):
        await s.append("u1", _run_with_result(i), run_id=f"r{i}")

    trimmed = await s.trim_tool_results("u1", keep_chars=400, keep_runs=1)
    assert trimmed == 2

    segs = await s.segments("u1")
    old_results = [segs[0].entries[2], segs[1].entries[2]]
    for result in old_results:
        assert result.output.startswith("r" * 400)
        assert 'recall_tool_result("c' in result.output
        assert result.raw is None
    # Structure preserved: same entry count, order, and call ids.
    assert all(len(seg.entries) == 4 for seg in segs)
    # The most recent run stays verbatim.
    assert segs[2].entries[2].output == "r" * 5_000


async def test_trim_is_idempotent(trim_session) -> None:
    s = trim_session
    for i in range(2):
        await s.append("u1", _run_with_result(i), run_id=f"r{i}")
    assert await s.trim_tool_results("u1", keep_runs=0) == 2
    after_first = [e.output for seg in await s.segments("u1") for e in seg.entries[2:3]]
    assert await s.trim_tool_results("u1", keep_runs=0) == 0  # nothing left to trim
    after_second = [
        e.output for seg in await s.segments("u1") for e in seg.entries[2:3]
    ]
    assert after_first == after_second


async def test_trim_skips_results_not_worth_trimming(trim_session) -> None:
    s = trim_session
    await s.append("u1", _run_with_result(0, chars=450), run_id="r0")
    await s.append("u1", [InputEntry(role="user", content="next")], run_id="r1")
    # 450 chars minus a ~140-char marker saves nothing; leave it verbatim.
    assert await s.trim_tool_results("u1", keep_chars=400, keep_runs=1) == 0
    assert (await s.segments("u1"))[0].entries[2].output == "r" * 450


async def test_trim_validates_arguments(trim_session) -> None:
    with pytest.raises(ValueError, match="keep_chars"):
        await trim_session.trim_tool_results("u1", keep_chars=-1)
    with pytest.raises(ValueError, match="keep_runs"):
        await trim_session.trim_tool_results("u1", keep_runs=-1)


async def test_sqlite_trim_rolls_back_partial_writes_on_error() -> None:
    # The ":memory:" connection is shared and outlives the call: without a
    # rollback, an update left uncommitted by a mid-trim failure would be
    # silently committed by the NEXT operation's commit().
    s = SQLiteSession(":memory:")
    for i in range(3):
        await s.append("u1", _run_with_result(i), run_id=f"r{i}")
    conn = s._connect()
    conn.execute(
        "UPDATE session_runs SET entries_json = 'not json' WHERE run_id = 'r1'"
    )
    conn.commit()

    # r0 is updated first, then r1's corrupt JSON raises mid-transaction.
    with pytest.raises(ValueError):
        await s.trim_tool_results("u1", keep_runs=0)

    # A follow-up write commits on the shared connection; r0's trim must not
    # ride along with it. (Read r0's row directly — r1 stays corrupt.)
    await s.append("u1", [InputEntry(role="user", content="later")], run_id="r3")
    row = conn.execute(
        "SELECT entries_json FROM session_runs WHERE run_id = 'r0'"
    ).fetchone()
    assert "r" * 5_000 in row[0]


async def test_in_memory_checkpointer_freezes_head_state() -> None:
    # The RunHead handed to append() aliases the run's *live* context_state
    # dict. The store must freeze it at append time (as SQLite does by
    # serializing immediately), and hand out copies on load so a caller
    # mutating the snapshot cannot corrupt the store.
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage
    from lovia.stores import InMemoryCheckpointer

    cp = InMemoryCheckpointer()
    live_state = {"summary": "v1", "offloaded": [1]}
    head = RunHead(agent_name="a", usage=Usage(), turns=1, context_state=live_state)
    await cp.append("r1", [InputEntry(role="user", content="hi")], head)

    live_state["summary"] = "v2"  # the run keeps mutating its scratch...
    live_state["offloaded"].append(2)  # ...including nested structures in place
    snap = await cp.load("r1")
    assert snap is not None
    assert snap.context_state == {"summary": "v1", "offloaded": [1]}  # frozen

    snap.context_state["summary"] = "vandalized"  # a careless reader
    snap.context_state["offloaded"].append(3)
    snap2 = await cp.load("r1")
    assert snap2 is not None
    assert snap2.context_state == {"summary": "v1", "offloaded": [1]}  # unharmed
