"""Design guarantees of the append-only Session / checkpoint split.

The Session is the log of completed runs; the checkpoint is the log of the
in-flight run; the full transcript is ``session.load() + snapshot.entries``.
These tests pin the contract that makes that split correct:

* each run appends its own entries to the Session as one segment;
* the checkpoint stores ONLY the run's own entries — never the prior history;
* resume reloads history from the Session and appends the run's delta exactly
  once (history stays immutable);
* the SQLite checkpoint grows append-by-append (one row per non-empty append,
  never rewritten) with a single head.
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
)
from lovia.checkpointer import RunHead
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

    # Append-only: one row per non-empty append (old rows never rewritten),
    # one head row.
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


async def test_handoff_to_systemless_agent_keeps_run_boundary() -> None:
    # The receiving agent has empty ``instructions`` (and no workspace/plugins/
    # structured-output), so it renders NO system entry. The handoff drops the
    # leading system entry and shifts every body entry left by one; ``run_start``
    # must follow (delta -1) or this run's segment loses its opening input. This
    # is one of the only paths that drives the ``run_start`` handoff delta
    # non-zero, so it guards the otherwise-untested edge.
    session = InMemorySession()
    # Seed prior history so run_start > 1 and any drift is observable.
    seed = Agent(name="seed", instructions="SEED", model=ScriptedProvider([text("r1")]))
    await Runner.run(seed, "first", session=session, session_id="u1")

    specialist = Agent(name="specialist", model=ScriptedProvider([text("final")]))
    assert specialist.instructions == ""  # => renders no system entry
    triage = Agent(
        name="triage",
        instructions="TRIAGE",  # => has a system entry
        model=ScriptedProvider(
            [call("transfer_to_specialist", {"reason": "x"}, call_id="c1")]
        ),
        handoffs=[Handoff(target=specialist)],
    )
    result = await Runner.run(triage, "second", session=session, session_id="u1")
    assert result.output == "final"

    segs = await session.segments("u1")
    assert len(segs) == 2
    # This run's input lands in its OWN segment exactly once; prior history is
    # not absorbed into it.
    seg2 = _contents(segs[1].entries)
    assert "second" in seg2
    assert "first" not in seg2 and "r1" not in seg2
    full = _contents(await session.load("u1"))
    assert [c for c in full if c in {"first", "r1", "second", "final"}] == [
        "first",
        "r1",
        "second",
        "final",
    ]


async def test_handoff_from_systemless_agent_keeps_run_boundary() -> None:
    # Reverse: the FIRST agent has empty ``instructions`` (no system entry) and
    # hands off to one WITH instructions, so a system entry *appears* mid-run.
    # ``run_start`` must shift the other way (delta +1) or the run's segment
    # absorbs the last history entry.
    session = InMemorySession()
    seed = Agent(name="seed", model=ScriptedProvider([text("r1")]))  # empty system
    await Runner.run(seed, "first", session=session, session_id="u1")

    specialist = Agent(
        name="specialist", instructions="SPEC", model=ScriptedProvider([text("final")])
    )
    triage = Agent(
        name="triage",  # empty instructions => no system entry
        model=ScriptedProvider(
            [call("transfer_to_specialist", {"reason": "x"}, call_id="c1")]
        ),
        handoffs=[Handoff(target=specialist)],
    )
    result = await Runner.run(triage, "second", session=session, session_id="u1")
    assert result.output == "final"

    segs = await session.segments("u1")
    assert len(segs) == 2
    seg2 = _contents(segs[1].entries)
    assert "second" in seg2
    assert "r1" not in seg2  # history not duplicated into this run's segment
    full = _contents(await session.load("u1"))
    assert [c for c in full if c in {"first", "r1", "second", "final"}] == [
        "first",
        "r1",
        "second",
        "final",
    ]
