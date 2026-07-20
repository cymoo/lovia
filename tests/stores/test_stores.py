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


# ------------------------------------------------- SQLiteCheckpointer store ---


def _head(turns: int = 1, status: str = "running"):
    from lovia.checkpointer import RunHead
    from lovia.messages import Usage

    return RunHead(agent_name="a", usage=Usage(), turns=turns, status=status)  # type: ignore[arg-type]


async def test_sqlite_checkpointer_append_rolls_back_partial_writes() -> None:
    # The ":memory:" connection is shared and outlives the call: without a
    # rollback, the turn row left uncommitted by a mid-append failure (between
    # the turn INSERT and the head INSERT) would ride the NEXT append's
    # commit() — duplicating the delta, so a resume would replay entries (and
    # re-execute tools) twice.
    import sqlite3

    from lovia.stores import SQLiteCheckpointer

    cp = SQLiteCheckpointer(":memory:")
    await cp.append("r1", [InputEntry(role="user", content="one")], _head(turns=1))

    conn = cp._connect()
    conn.execute("ALTER TABLE snapshot_heads RENAME TO snapshot_heads_gone")
    conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        await cp.append("r1", [InputEntry(role="user", content="two")], _head(turns=2))
    conn.execute("ALTER TABLE snapshot_heads_gone RENAME TO snapshot_heads")
    conn.commit()

    # The retry (as save_terminal would do with the same delta) must store the
    # delta exactly once.
    await cp.append("r1", [InputEntry(role="user", content="two")], _head(turns=2))
    snap = await cp.load("r1")
    assert snap is not None
    assert [e.content for e in snap.entries] == ["one", "two"]  # type: ignore[union-attr]


async def test_checkpointer_head_only_append_adds_no_turn_row() -> None:
    # An empty-entries append (e.g. marking completion) refreshes the head
    # without growing the entry log.
    from lovia.stores import SQLiteCheckpointer

    cp = SQLiteCheckpointer(":memory:")
    await cp.append("r1", [InputEntry(role="user", content="hi")], _head(turns=1))
    await cp.append("r1", [], _head(turns=1, status="completed"))

    snap = await cp.load("r1")
    assert snap is not None
    assert snap.status == "completed"
    assert [e.content for e in snap.entries] == ["hi"]  # type: ignore[union-attr]
    conn = cp._connect()
    rows = conn.execute("SELECT COUNT(*) FROM snapshot_turns").fetchone()[0]
    assert rows == 1


async def test_sqlite_stores_keep_non_ascii_verbatim() -> None:
    # ensure_ascii=False: CJK content round-trips AND is stored unescaped
    # (readable, and roughly half the bytes of \\uXXXX escapes).
    from lovia.stores import SQLiteCheckpointer

    s = SQLiteSession(":memory:")
    await s.append(
        "u1",
        [InputEntry(role="user", content="你好，世界")],
        run_id="r1",
        meta={"备注": "中文元数据"},
    )
    segs = await s.segments("u1")
    assert segs[0].entries[0].content == "你好，世界"  # type: ignore[union-attr]
    assert segs[0].meta == {"备注": "中文元数据"}
    row = (
        s._connect()
        .execute("SELECT entries_json, meta_json FROM session_runs WHERE run_id = 'r1'")
        .fetchone()
    )
    assert "你好，世界" in row[0] and "中文元数据" in row[1]

    cp = SQLiteCheckpointer(":memory:")
    await cp.append("r1", [AssistantTextEntry(content="答案是四十二")], _head())
    snap = await cp.load("r1")
    assert snap is not None
    assert snap.entries[0].content == "答案是四十二"  # type: ignore[union-attr]
    turn_row = (
        cp._connect().execute("SELECT entries_json FROM snapshot_turns").fetchone()
    )
    assert "答案是四十二" in turn_row[0]


async def test_same_run_id_under_two_sessions_both_stored() -> None:
    # Idempotency is scoped to (session_id, run_id), not run_id alone.
    s = SQLiteSession(":memory:")
    await s.append("u1", [InputEntry(role="user", content="one")], run_id="r")
    await s.append("u2", [InputEntry(role="user", content="two")], run_id="r")
    assert [e.content for e in await s.load("u1")] == ["one"]  # type: ignore[union-attr]
    assert [e.content for e in await s.load("u2")] == ["two"]  # type: ignore[union-attr]


async def test_wal_option_enables_wal_journal_mode(tmp_path: Path) -> None:
    from lovia.stores import SQLiteCheckpointer

    s = SQLiteSession(tmp_path / "wal.db", wal=True)
    await s.append("u1", [InputEntry(role="user", content="hi")], run_id="r1")
    assert [e.content for e in await s.load("u1")] == ["hi"]  # type: ignore[union-attr]
    with s._conn() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    # Default stays off (SQLite's default journal mode, not WAL).
    plain = SQLiteSession(tmp_path / "plain.db")
    await plain.append("u1", [InputEntry(role="user", content="hi")])
    with plain._conn() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"

    cp = SQLiteCheckpointer(tmp_path / "wal.db", wal=True)
    await cp.append("r1", [InputEntry(role="user", content="hi")], _head())
    snap = await cp.load("r1")
    assert snap is not None and len(snap.entries) == 1


# ------------------------------------------------------------------ rewind ---


@pytest.fixture(params=["memory", "sqlite"])
def rw_session(request, tmp_path):
    if request.param == "memory":
        return InMemorySession()
    return SQLiteSession(tmp_path / "rewind.db")


def _turn(i: int) -> list:
    """One user→assistant run: [user u{i}, assistant a{i}]."""
    return [
        InputEntry(role="user", content=f"u{i}"),
        AssistantTextEntry(content=f"a{i}"),
    ]


async def test_rewind_drops_whole_later_runs(rw_session) -> None:
    s = rw_session
    for i in range(3):
        await s.append("s", _turn(i), run_id=f"r{i}", meta={"note": i})

    removed = await s.rewind("s", keep_entries=4)  # cut on the r1/r2 edge
    assert removed == 2
    entries = await s.load("s")
    assert [e.content for e in entries] == ["u0", "a0", "u1", "a1"]
    # Kept runs survive whole — segments, run ids, and metas untouched.
    segs = await s.segments("s")
    assert [seg.run_id for seg in segs] == ["r0", "r1"]
    assert [seg.meta for seg in segs] == [{"note": 0}, {"note": 1}]


async def test_rewind_truncates_inside_a_run_and_drops_its_meta(rw_session) -> None:
    s = rw_session
    await s.append("s", _turn(0), run_id="r0", meta={"note": 0})
    # r1 holds two user turns (a mid-run injection).
    await s.append("s", _turn(1) + _turn(2), run_id="r1", meta={"note": 1})

    removed = await s.rewind("s", keep_entries=4)  # keep r0 + r1's first turn
    assert removed == 2
    entries = await s.load("s")
    assert [e.content for e in entries] == ["u0", "a0", "u1", "a1"]
    segs = await s.segments("s")
    assert [seg.run_id for seg in segs] == ["r0", "r1"]
    assert segs[0].meta == {"note": 0}
    # The truncated run's meta was computed after content that no longer
    # exists (carried context state) — it must not survive the cut.
    assert segs[1].meta is None


async def test_rewind_drops_dangling_tool_call_at_the_cut(rw_session) -> None:
    s = rw_session
    await s.append(
        "s",
        [
            InputEntry(role="user", content="u"),
            ToolCallEntry(call_id="c1", name="f", arguments="{}"),
            ToolResultEntry(call_id="c1", output="r"),
            AssistantTextEntry(content="a"),
        ],
        run_id="r0",
    )
    # Cutting between the call and its result must not leave the dangling
    # call behind (providers reject a tool_use without its result).
    removed = await s.rewind("s", keep_entries=2)
    assert removed == 3
    entries = await s.load("s")
    assert [type(e).__name__ for e in entries] == ["InputEntry"]


async def test_rewind_boundary_truncated_to_nothing_drops_the_run(rw_session) -> None:
    s = rw_session
    await s.append("s", _turn(0), run_id="r0")
    await s.append(
        "s",
        [
            ToolCallEntry(call_id="c1", name="f", arguments="{}"),
            ToolResultEntry(call_id="c1", output="r"),
        ],
        run_id="r1",
    )
    # Keeping only r1's dangling call leaves nothing valid — the run goes too.
    removed = await s.rewind("s", keep_entries=3)
    assert removed == 2
    assert [seg.run_id for seg in await s.segments("s")] == ["r0"]


async def test_rewind_to_zero_empties_the_session(rw_session) -> None:
    s = rw_session
    await s.append("s", _turn(0))
    assert await s.rewind("s", keep_entries=0) == 2
    assert await s.load("s") == []
    assert await s.segments("s") == []


async def test_rewind_past_the_end_is_a_noop(rw_session) -> None:
    s = rw_session
    await s.append("s", _turn(0), run_id="r0", meta={"note": 0})
    assert await s.rewind("s", keep_entries=2) == 0
    assert await s.rewind("s", keep_entries=99) == 0
    assert await s.rewind("missing", keep_entries=0) == 0
    segs = await s.segments("s")
    assert len(segs) == 1 and segs[0].meta == {"note": 0}


async def test_rewind_validates_arguments(rw_session) -> None:
    with pytest.raises(ValueError, match="keep_entries"):
        await rw_session.rewind("s", keep_entries=-1)


async def test_rewind_survives_sqlite_reopen(tmp_path) -> None:
    path = tmp_path / "rewound.db"
    s = SQLiteSession(path)
    for i in range(2):
        await s.append("s", _turn(i), run_id=f"r{i}")
    await s.rewind("s", keep_entries=2)

    reopened = SQLiteSession(path)
    entries = await reopened.load("s")
    assert [e.content for e in entries] == ["u0", "a0"]
