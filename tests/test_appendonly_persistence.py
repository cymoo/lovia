"""Design guarantees of the append-only Session / checkpoint split.

The Session is the log of completed runs; the checkpoint is the log of the
in-flight run; the full transcript is ``session.load() + snapshot.entries``.
These tests pin the contract that makes that split correct:

* each run appends its own entries to the Session as one segment;
* the checkpoint stores ONLY the run's own entries — never the prior history;
* resume reloads history from the Session and appends the run's delta exactly
  once (history stays immutable);
* the SQLite checkpoint grows turn-by-turn (append-only) with a single head.
"""

from __future__ import annotations

from typing import Any

from lovia import (
    Agent,
    CheckpointOptions,
    Handoff,
    InMemoryCheckpointer,
    InMemorySession,
    Runner,
    tool,
)
from lovia.checkpointer import RunHead
from lovia.handoff import drop_stale_tool_calls
from lovia.messages import Usage
from lovia.stores import SQLiteCheckpointer
from lovia.transcript import AssistantTextEntry, InputEntry

from .scripted_provider import ScriptedProvider, call, text


def ckpt(cp: Any, run_id: str, **kwargs: Any) -> CheckpointOptions:
    return CheckpointOptions(cp, run_id, **kwargs)


def _contents(entries: list[Any]) -> list[Any]:
    return [getattr(e, "content", None) for e in entries]


async def test_session_persists_each_run_as_its_own_segment() -> None:
    session = InMemorySession()
    agent = Agent(name="a", model=ScriptedProvider([text("hi"), text("Mei")]))

    await Runner.run(agent, "I'm Mei", session=session, session_id="u1")
    await Runner.run(agent, "my name?", session=session, session_id="u1")

    # Two runs -> two segments; load concatenates them in order, and the second
    # run saw the first run's history (no full rewrite anywhere).
    assert len(session._segments["u1"]) == 2
    assert _contents(await session.load("u1")) == ["I'm Mei", "hi", "my name?", "Mei"]


async def test_checkpoint_stores_only_the_runs_own_entries_not_history() -> None:
    session = InMemorySession()
    cp = InMemoryCheckpointer()

    # Seed history with a completed run.
    await Runner.run(
        Agent(name="a", model=ScriptedProvider([text("hello")])),
        "hi",
        session=session,
        session_id="u1",
    )
    # A checkpointed run on the same session.
    agent = Agent(name="a", model=ScriptedProvider([text("world")]))
    await Runner.run(
        agent, "again", session=session, session_id="u1", checkpoint=ckpt(cp, "r1")
    )

    snap = await cp.load("r1")
    assert snap is not None
    # The checkpoint holds ONLY this run's entries — the prior "hi"/"hello"
    # history is in the Session, never duplicated into the snapshot.
    assert _contents(snap.entries) == ["again", "world"]


async def test_resume_reloads_history_and_appends_run_delta_once() -> None:
    session = InMemorySession()
    cp = InMemoryCheckpointer()

    # r0: a completed run seeds the session history.
    await Runner.run(
        Agent(name="a", model=ScriptedProvider([text("hello")])),
        "hi",
        session=session,
        session_id="u1",
    )

    # r1: an interrupted run whose own entries so far are just its input.
    await cp.append(
        "r1",
        [InputEntry(role="user", content="again")],
        RunHead(agent_name="a", usage=Usage(), turns=1, status="interrupted"),
    )
    assert len(session._segments["u1"]) == 1  # r1 is not in the session yet

    agent = Agent(name="a", model=ScriptedProvider([text("recovered")]))
    result = await Runner.run(
        agent,
        [],
        session=session,
        session_id="u1",
        checkpoint=ckpt(cp, "r1", if_run_exists="resume_only"),
    )

    assert result.output == "recovered"
    # The resumed run saw the prior history reloaded from the Session...
    assert _contents(result.entries) == ["hi", "hello", "again", "recovered"]
    # ...and appended exactly its own entries as one new segment; the existing
    # history segment is untouched (immutable, appended once).
    assert len(session._segments["u1"]) == 2
    assert _contents(await session.load("u1")) == [
        "hi",
        "hello",
        "again",
        "recovered",
    ]


async def test_no_session_run_is_self_contained_in_the_checkpoint() -> None:
    # Without a Session there is no history to merge: the checkpoint's entries
    # ARE the full transcript, so resume stands alone.
    cp = InMemoryCheckpointer()
    await cp.append(
        "solo",
        [InputEntry(role="user", content="ping")],
        RunHead(agent_name="a", usage=Usage(), turns=1, status="interrupted"),
    )
    agent = Agent(name="a", model=ScriptedProvider([text("pong")]))
    result = await Runner.run(
        agent, [], checkpoint=ckpt(cp, "solo", if_run_exists="resume_only")
    )
    assert result.output == "pong"
    assert _contents(result.entries) == ["ping", "pong"]


async def test_sqlite_checkpoint_appends_turns_incrementally(tmp_path: Any) -> None:
    cp = SQLiteCheckpointer(str(tmp_path / "c.db"))

    await cp.append(
        "r1",
        [InputEntry(role="user", content="a")],
        RunHead(agent_name="x", usage=Usage(), turns=1),
    )
    await cp.append(
        "r1",
        [AssistantTextEntry(content="b")],
        RunHead(agent_name="x", usage=Usage(), turns=2, status="completed"),
    )

    # Append-only: one row per turn (old rows never rewritten), one head row.
    conn = cp._connect()
    try:
        n_turns = conn.execute(
            "SELECT COUNT(*) FROM snapshot_turns WHERE run_id = 'r1'"
        ).fetchone()[0]
        n_heads = conn.execute(
            "SELECT COUNT(*) FROM snapshot_heads WHERE run_id = 'r1'"
        ).fetchone()[0]
    finally:
        cp._release(conn)
    assert (n_turns, n_heads) == (2, 1)

    snap = await cp.load("r1")
    assert snap is not None
    assert _contents(snap.entries) == ["a", "b"]
    assert snap.status == "completed"
    assert snap.turns == 2

    await cp.delete("r1")
    assert await cp.load("r1") is None


async def test_session_append_runs_after_checkpoint_is_finalized() -> None:
    """A run must live in exactly one place. If the Session append ran before the
    checkpoint was finalized, a crash between them would leave the run both
    persisted AND resumable — and resume (which reloads history from the Session)
    would double-count it. So the append must come after checkpoint completion."""
    order: list[str] = []

    class _OrderSession(InMemorySession):
        async def append(self, session_id, entries, *, run_id=None, meta=None):  # type: ignore[override]
            order.append("session")
            return await super().append(session_id, entries, run_id=run_id, meta=meta)

    class _OrderCheckpointer(InMemoryCheckpointer):
        async def append(self, run_id, entries, head):  # type: ignore[override]
            if head.status == "completed":
                order.append("checkpoint-final")
            await super().append(run_id, entries, head)

        async def delete(self, run_id):  # type: ignore[override]
            order.append("checkpoint-final")
            await super().delete(run_id)

    agent = Agent(name="a", model=ScriptedProvider([text("ok")]))
    await Runner.run(
        agent,
        "hi",
        session=_OrderSession(),
        session_id="u1",
        checkpoint=ckpt(_OrderCheckpointer(), "r1"),
    )
    assert "session" in order and "checkpoint-final" in order
    assert order.index("session") > order.index("checkpoint-final")


async def test_restart_checkpoint_keeps_only_the_new_runs_entries() -> None:
    cp = InMemoryCheckpointer()
    agent = Agent(name="a", model=ScriptedProvider([text("first"), text("second")]))
    await Runner.run(agent, "one", checkpoint=ckpt(cp, "job"))
    await Runner.run(agent, "two", checkpoint=ckpt(cp, "job", if_run_exists="restart"))

    snap = await cp.load("job")
    assert snap is not None
    # Restart discards the prior run's turn-rows; only the new run remains
    # (no concatenation of old + new entries).
    assert _contents(snap.entries) == ["two", "second"]


async def test_handoff_input_filter_persists_unfiltered_entries_to_session() -> None:
    @tool
    def add(a: int, b: int) -> int:
        return a + b

    specialist = Agent(name="specialist", model=ScriptedProvider([text("final")]))
    triage = Agent(
        name="triage",
        model=ScriptedProvider(
            [
                call("add", {"a": 1, "b": 2}, call_id="c1"),
                call("transfer_to_specialist", {"reason": "x"}, call_id="c2"),
            ]
        ),
        tools=[add],
        handoffs=[Handoff(target=specialist, input_filter=drop_stale_tool_calls)],
    )
    session = InMemorySession()
    await Runner.run(triage, "go", session=session, session_id="u1")

    kinds = [type(e).__name__ for e in await session.load("u1")]
    # The specialist's per-call VIEW had the tool call/result filtered out, but
    # the Session records the run's TRUE, unfiltered contribution as one segment.
    assert "ToolCallEntry" in kinds and "ToolResultEntry" in kinds
    assert len(session._segments["u1"]) == 1
