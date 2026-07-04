"""Tests for the checkpointer protocol and snapshot round-tripping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

import pytest
from pydantic import BaseModel

from lovia import (
    Agent,
    CheckpointOptions,
    ImagePart,
    InMemoryCheckpointer,
    ProviderError,
    RetryPolicy,
    RunContext,
    Runner,
    TextPart,
    events,
    tool,
)
from lovia.checkpointer import RunHead, RunSnapshot
from lovia.transcript import (
    FinishDelta,
    InputEntry,
    AssistantTextEntry,
    ModelDelta,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
    TextDelta,
    UsageDelta,
)
from lovia.messages import Usage
from lovia.stores.checkpointer import SQLiteCheckpointer

from .scripted_provider import ScriptedProvider, call, text


def ckpt(cp: Any, run_id: str, **kwargs: Any) -> CheckpointOptions:
    return CheckpointOptions(cp, run_id, **kwargs)


async def _seed(cp: Any, snap: RunSnapshot) -> None:
    """Seed a checkpointer with a whole snapshot via the append API."""
    await cp.append(snap.run_id, snap.entries, snap.head)


class RecordingCheckpointer(InMemoryCheckpointer):
    def __init__(self) -> None:
        super().__init__()
        self.saved: list[RunSnapshot] = []

    async def append(
        self, run_id: str, entries: list[TranscriptEntry], head: RunHead
    ) -> None:
        await super().append(run_id, entries, head)
        snap = await self.load(run_id)
        assert snap is not None
        self.saved.append(snap)


class FlakyProvider:
    name = "flaky"
    supports_json_schema = False

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, *a: Any, **kw: Any) -> AsyncIterator[ModelDelta]:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("temporary outage", retryable=True)
        yield TextDelta(text="recovered")
        yield UsageDelta(usage=Usage(input_tokens=1, output_tokens=1))
        yield FinishDelta(reason="stop")


def test_checkpoint_options_validates_configuration() -> None:
    cp = InMemoryCheckpointer()
    snap = RunSnapshot(
        run_id="snap",
        agent_name="a",
        entries=[InputEntry(role="user", content="hi")],
        usage=Usage(),
        turns=0,
    )

    with pytest.raises(Exception, match="run_id"):
        CheckpointOptions(checkpointer=cp)
    with pytest.raises(Exception, match="checkpointer"):
        CheckpointOptions(run_id="r")
    with pytest.raises(Exception, match="non-empty"):
        CheckpointOptions(cp, "")
    with pytest.raises(Exception, match="does not match"):
        CheckpointOptions(cp, "other", resume_from=snap)
    with pytest.raises(Exception, match="if_run_exists"):
        CheckpointOptions(cp, "r", if_run_exists="bogus")  # type: ignore[arg-type]

    direct = CheckpointOptions(resume_from=snap)
    assert direct.resolved_run_id == "snap"


@pytest.mark.asyncio
async def test_checkpointer_snapshot_round_trip() -> None:
    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text("hello there")])
    agent = Agent(name="a", model=provider)
    result = await Runner.run(agent, "hi", checkpoint=ckpt(cp, "r1"))
    assert result.output == "hello there"

    snap = await cp.load("r1")
    assert snap is not None
    assert snap.run_id == "r1"
    assert snap.agent_name == "a"
    assert snap.status == "completed"
    assert snap.output == "hello there"
    # The assistant turn shows up as a AssistantTextEntry in the snapshot.
    assert any(isinstance(it, AssistantTextEntry) for it in snap.entries)
    assert snap.usage.output_tokens > 0


@pytest.mark.asyncio
async def test_resume_completed_snapshot_returns_without_rerunning_provider() -> None:
    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="a", model=provider)

    await Runner.run(agent, "hi", checkpoint=ckpt(cp, "done"))
    result = await Runner.run(
        agent, [], checkpoint=ckpt(cp, "done", if_run_exists="resume_only")
    )

    assert result.output == "done"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_resume_from_snapshot_option_directly() -> None:
    entries = [
        InputEntry(role="user", content="What is the time?"),
        ToolCallEntry(call_id="c1", name="clock", arguments="{}"),
        ToolResultEntry(call_id="c1", output="12:00"),
    ]
    snap = RunSnapshot(
        run_id="direct",
        agent_name="a",
        entries=entries,
        usage=Usage(input_tokens=10, output_tokens=5),
        turns=1,
    )
    provider = ScriptedProvider([text("It is noon.")])
    agent = Agent(name="a", model=provider)

    result = await Runner.run(agent, [], checkpoint=CheckpointOptions(resume_from=snap))

    assert result.output == "It is noon."
    assert result.entries[:3] == entries
    assert result.usage.input_tokens >= 10


@pytest.mark.asyncio
async def test_resume_completed_snapshot_can_delete_checkpoint() -> None:
    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="a", model=provider)

    await Runner.run(agent, "hi", checkpoint=ckpt(cp, "done-delete"))
    result = await Runner.run(
        agent,
        [],
        checkpoint=ckpt(
            cp,
            "done-delete",
            delete_on_success=True,
            if_run_exists="resume_only",
        ),
    )

    assert result.output == "done"
    assert await cp.load("done-delete") is None


@pytest.mark.asyncio
async def test_resume_completed_structured_snapshot_rehydrates_output() -> None:
    class Out(BaseModel):
        value: int

    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text('{"value": 3}')])
    agent = Agent(name="a", model=provider, output_type=Out)

    await Runner.run(agent, "hi", checkpoint=ckpt(cp, "typed"))
    result = await Runner.run(
        agent, [], checkpoint=ckpt(cp, "typed", if_run_exists="resume_only")
    )

    assert isinstance(result.output, Out)
    assert result.output.value == 3
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_resume_completed_snapshot_with_run_level_output_type() -> None:
    class Out(BaseModel):
        value: int

    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text('{"value": 3}')])
    agent = Agent(name="a", model=provider)

    await Runner.run(agent, "hi", output_type=Out, checkpoint=ckpt(cp, "override"))
    result = await Runner.run(
        agent,
        [],
        checkpoint=ckpt(cp, "override", if_run_exists="resume_only"),
        output_type=Out,
    )
    assert isinstance(result.output, Out)
    assert result.output.value == 3
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_resume_completed_snapshot_rejects_unserializable_output() -> None:
    cp = InMemoryCheckpointer()
    await _seed(
        cp,
        RunSnapshot(
            run_id="bad-output",
            agent_name="a",
            entries=[InputEntry(role="user", content="hi")],
            usage=Usage(),
            turns=1,
            status="completed",
            output=None,
            error={
                "type": "OutputNotSerializable",
                "message": "could not serialize",
            },
        ),
    )
    agent = Agent(name="a", model=ScriptedProvider([]))

    with pytest.raises(Exception, match="not JSON-safe"):
        await Runner.run(
            agent, [], checkpoint=ckpt(cp, "bad-output", if_run_exists="resume_only")
        )


@pytest.mark.asyncio
async def test_resume_completed_snapshot_appends_session_idempotently() -> None:
    # Replaying a completed snapshot re-applies session persistence keyed by
    # run_id. When the original completion already appended, that's a no-op;
    # replaying repeatedly never duplicates the segment.
    from lovia.stores import InMemorySession

    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="a", model=provider)
    session = InMemorySession()
    await Runner.run(
        agent,
        "hi",
        checkpoint=ckpt(cp, "done-session"),
        session=session,
        session_id="s1",
    )
    assert len(await session.segments("s1")) == 1

    for _ in range(2):  # replay twice; the segment must not duplicate
        result = await Runner.run(
            agent,
            [],
            checkpoint=ckpt(cp, "done-session", if_run_exists="resume_only"),
            session=session,
            session_id="s1",
        )
        assert result.output == "done"

    segs = await session.segments("s1")
    assert len(segs) == 1
    assert segs[0].run_id == "done-session"
    assert len(provider.calls) == 1  # the model was never re-invoked


@pytest.mark.asyncio
async def test_replay_heals_session_lost_in_the_crash_window() -> None:
    # The loop finalizes the checkpoint BEFORE appending to the session. A
    # crash (or store error) between the two used to lose the run's entries
    # from the session forever — the checkpoint said "completed" so a re-issue
    # only replayed the result. Replay now re-appends idempotently, healing
    # the window.
    from lovia.stores import InMemorySession

    class FlakySession(InMemorySession):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_append = True

        async def append(self, session_id, entries, *, run_id=None, meta=None):  # type: ignore[override]
            if self.fail_next_append:
                self.fail_next_append = False
                raise ConnectionError("session store down")
            return await super().append(session_id, entries, run_id=run_id, meta=meta)

    cp = InMemoryCheckpointer()
    session = FlakySession()
    agent = Agent(name="a", model=ScriptedProvider([text("answer")]))

    with pytest.raises(ConnectionError):
        await Runner.run(
            agent,
            "q",
            checkpoint=ckpt(cp, "rA"),
            session=session,
            session_id="s1",
        )
    snap = await cp.load("rA")
    assert snap is not None and snap.status == "completed"
    assert await session.segments("s1") == []  # the crash window

    # Idempotent re-issue of the same run: replays the result AND heals the
    # session.
    agent2 = Agent(name="a", model=ScriptedProvider([]))  # model must not run
    result = await Runner.run(
        agent2,
        "q",
        checkpoint=ckpt(cp, "rA"),
        session=session,
        session_id="s1",
    )
    assert result.output == "answer"
    [seg] = await session.segments("s1")
    assert seg.run_id == "rA"
    assert seg.entries == snap.entries


@pytest.mark.asyncio
async def test_run_idempotent_resumes_existing_run() -> None:
    # Re-issuing the same run(...) after a crash resumes the stored run rather
    # than restarting (if_run_exists defaults to "resume").
    cp = InMemoryCheckpointer()
    provider = FlakyProvider()
    agent = Agent(name="a", model=provider)

    # max_attempts=1 disables in-process retry so the transient failure surfaces
    # and gets checkpointed; the resume path (not retry) is what's under test.
    with pytest.raises(ProviderError):
        await Runner.run(
            agent, "hi", checkpoint=ckpt(cp, "job"), retry=RetryPolicy(max_attempts=1)
        )

    result = await Runner.run(agent, "hi", checkpoint=ckpt(cp, "job"))
    assert result.output == "recovered"
    assert provider.calls == 2  # resumed; did not restart from turn 0


@pytest.mark.asyncio
async def test_run_idempotent_replays_completed_run() -> None:
    # A completed run is replayed; the new input is dropped and the model is
    # not called again.
    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text("done")])
    agent = Agent(name="a", model=provider)

    first = await Runner.run(agent, "hi", checkpoint=ckpt(cp, "job"))
    assert first.output == "done"

    again = await Runner.run(agent, "different", checkpoint=ckpt(cp, "job"))
    assert again.output == "done"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_if_run_exists_fail_raises_on_existing() -> None:
    cp = InMemoryCheckpointer()
    agent = Agent(name="a", model=ScriptedProvider([text("done")]))

    await Runner.run(agent, "hi", checkpoint=ckpt(cp, "job"))
    with pytest.raises(Exception, match="already exists"):
        await Runner.run(agent, "hi", checkpoint=ckpt(cp, "job", if_run_exists="fail"))


@pytest.mark.asyncio
async def test_if_run_exists_restart_overwrites() -> None:
    cp = InMemoryCheckpointer()
    provider = ScriptedProvider([text("first"), text("second")])
    agent = Agent(name="a", model=provider)

    first = await Runner.run(agent, "hi", checkpoint=ckpt(cp, "job"))
    assert first.output == "first"

    again = await Runner.run(
        agent, "hi", checkpoint=ckpt(cp, "job", if_run_exists="restart")
    )
    assert again.output == "second"
    assert len(provider.calls) == 2  # ran fresh both times


@pytest.mark.asyncio
async def test_run_failure_saves_failed_snapshot() -> None:
    cp = InMemoryCheckpointer()
    agent = Agent(name="a", model=ScriptedProvider([]))

    with pytest.raises(AssertionError):
        await Runner.run(agent, "hi", checkpoint=ckpt(cp, "failed"))

    snap = await cp.load("failed")
    assert snap is not None
    assert snap.status == "failed"
    assert snap.error is not None
    assert snap.error["type"] == "AssertionError"


@pytest.mark.asyncio
async def test_retryable_provider_failure_saves_interrupted_snapshot() -> None:
    cp = InMemoryCheckpointer()
    provider = FlakyProvider()
    agent = Agent(name="a", model=provider)

    # max_attempts=1 disables in-process retry so the transient failure surfaces
    # and gets checkpointed; the resume path (not retry) is what's under test.
    with pytest.raises(ProviderError):
        await Runner.run(
            agent,
            "hi",
            checkpoint=ckpt(cp, "interrupted"),
            retry=RetryPolicy(max_attempts=1),
        )

    snap = await cp.load("interrupted")
    assert snap is not None
    assert snap.status == "interrupted"
    assert snap.turns == 0
    assert snap.error is not None
    assert snap.error["type"] == "ProviderError"

    result = await Runner.run(
        agent, [], checkpoint=ckpt(cp, "interrupted", if_run_exists="resume_only")
    )
    assert result.output == "recovered"
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_resume_streams_events() -> None:
    # if_run_exists="resume_only" returns a RunHandle, so a resumed run can be
    # consumed as a live event stream — not just awaited for the RunResult.
    cp = InMemoryCheckpointer()
    provider = FlakyProvider()
    agent = Agent(name="a", model=provider)

    # max_attempts=1 disables in-process retry so the transient failure surfaces
    # and gets checkpointed; the resume path (not retry) is what's under test.
    with pytest.raises(ProviderError):
        await Runner.run(
            agent,
            "hi",
            checkpoint=ckpt(cp, "stream-resume"),
            retry=RetryPolicy(max_attempts=1),
        )

    handle = Runner.stream(
        agent, [], checkpoint=ckpt(cp, "stream-resume", if_run_exists="resume_only")
    )
    seen: list[type] = []
    async for ev in handle:
        seen.append(type(ev))

    assert events.RunStarted in seen
    assert events.RunCompleted in seen
    result = await handle.result()
    assert result.output == "recovered"


@pytest.mark.asyncio
async def test_resume_only_missing_run_id_raises_when_driven() -> None:
    # The snapshot loads lazily, so a missing run_id surfaces when the handle
    # is first driven rather than from the stream() call itself.
    cp = InMemoryCheckpointer()
    agent = Agent(name="a", model=ScriptedProvider([]))

    handle = Runner.stream(
        agent, [], checkpoint=ckpt(cp, "missing", if_run_exists="resume_only")
    )
    with pytest.raises(Exception, match="No snapshot found"):
        await handle


@pytest.mark.asyncio
async def test_success_can_delete_checkpoint() -> None:
    cp = InMemoryCheckpointer()
    agent = Agent(name="a", model=ScriptedProvider([text("done")]))

    await Runner.run(
        agent,
        "hi",
        checkpoint=ckpt(cp, "delete-me", delete_on_success=True),
    )

    assert await cp.load("delete-me") is None


@pytest.mark.asyncio
async def test_resume_continues_from_snapshot() -> None:
    cp = InMemoryCheckpointer()
    entries = [
        InputEntry(role="user", content="What is the time?"),
        ToolCallEntry(call_id="c1", name="clock", arguments="{}"),
        ToolResultEntry(call_id="c1", output="12:00"),
    ]
    await _seed(
        cp,
        RunSnapshot(
            run_id="r2",
            agent_name="a",
            entries=entries,
            usage=Usage(input_tokens=10, output_tokens=5),
            turns=1,
        ),
    )

    @tool
    async def clock() -> str:
        return "12:00"

    provider = ScriptedProvider([text("It is noon.")])
    agent = Agent(name="a", model=provider, tools=[clock])

    result = await Runner.run(
        agent, [], checkpoint=ckpt(cp, "r2", if_run_exists="resume_only")
    )
    assert result.output == "It is noon."
    # The first three entries survive the resume verbatim.
    assert result.entries[:3] == entries
    assert result.usage.input_tokens >= 10


@pytest.mark.asyncio
async def test_resume_drains_pending_tool_calls_from_snapshot() -> None:
    cp = InMemoryCheckpointer()
    entries = [
        InputEntry(role="user", content="What is the time?"),
        ToolCallEntry(call_id="c1", name="clock", arguments="{}"),
    ]
    await _seed(
        cp,
        RunSnapshot(
            run_id="pending-tool",
            agent_name="a",
            entries=entries,
            usage=Usage(input_tokens=10, output_tokens=5),
            turns=1,
        ),
    )
    calls = 0

    @tool
    async def clock() -> str:
        nonlocal calls
        calls += 1
        return "12:00"

    provider = ScriptedProvider([text("It is noon.")])
    agent = Agent(name="a", model=provider, tools=[clock])

    result = await Runner.run(
        agent, [], checkpoint=ckpt(cp, "pending-tool", if_run_exists="resume_only")
    )

    assert result.output == "It is noon."
    assert calls == 1
    assert any(
        isinstance(entry, ToolResultEntry) and entry.call_id == "c1"
        for entry in result.entries
    )


@pytest.mark.asyncio
async def test_drained_pending_calls_see_the_restored_turn() -> None:
    # Tools executed while draining a resumed snapshot's pending calls must
    # see the snapshot's turn on ctx.turn (the turn they belong to), not the
    # RunContext default of 0.
    cp = InMemoryCheckpointer()
    await _seed(
        cp,
        RunSnapshot(
            run_id="turn-probe",
            agent_name="a",
            entries=[
                InputEntry(role="user", content="go"),
                ToolCallEntry(call_id="c1", name="probe", arguments="{}"),
            ],
            usage=Usage(input_tokens=10, output_tokens=5),
            turns=3,
        ),
    )
    seen: list[int] = []

    @tool
    async def probe(ctx: RunContext) -> str:
        seen.append(ctx.turn)
        return "ok"

    agent = Agent(name="a", model=ScriptedProvider([text("done")]), tools=[probe])
    result = await Runner.run(
        agent, [], checkpoint=ckpt(cp, "turn-probe", if_run_exists="resume_only")
    )

    assert result.output == "done"
    assert seen == [3]


@pytest.mark.asyncio
async def test_replay_completed_run_survives_a_changed_handoff_graph() -> None:
    # Replay needs the recorded agent only for attribution. When the handoff
    # graph changed since the run completed (agent renamed/removed), replay
    # degrades to the entry agent with a warning — a worker re-issuing
    # idempotent run_ids across a deploy must not error on finished runs.
    cp = InMemoryCheckpointer()
    await _seed(
        cp,
        RunSnapshot(
            run_id="legacy-done",
            agent_name="renamed-away",
            entries=[
                InputEntry(role="user", content="hi"),
                AssistantTextEntry(content="done"),
            ],
            usage=Usage(input_tokens=1, output_tokens=1),
            turns=1,
            status="completed",
            output="done",
        ),
    )
    agent = Agent(name="a", model=ScriptedProvider([]))  # model must not run

    result = await Runner.run(
        agent, [], checkpoint=ckpt(cp, "legacy-done", if_run_exists="resume_only")
    )

    assert result.output == "done"
    assert result.final_agent.name == "a"  # attributed to the entry agent


@pytest.mark.asyncio
async def test_resuming_a_running_snapshot_still_requires_a_reachable_agent() -> None:
    # The completed-replay fallback above must NOT extend to resumable
    # snapshots: continuing *execution* as the wrong agent is dangerous.
    cp = InMemoryCheckpointer()
    await _seed(
        cp,
        RunSnapshot(
            run_id="legacy-running",
            agent_name="renamed-away",
            entries=[InputEntry(role="user", content="hi")],
            usage=Usage(),
            turns=1,
            status="running",
        ),
    )
    agent = Agent(name="a", model=ScriptedProvider([]))

    with pytest.raises(Exception, match="not reachable"):
        await Runner.run(
            agent,
            [],
            checkpoint=ckpt(cp, "legacy-running", if_run_exists="resume_only"),
        )


@pytest.mark.asyncio
async def test_handoff_snapshot_records_target_agent_after_switch() -> None:
    cp = RecordingCheckpointer()
    spanish = Agent(
        name="Spanish",
        instructions="reply in Spanish",
        model=ScriptedProvider([text("Hola!")]),
    )
    english = Agent(
        name="English",
        instructions="reply in English",
        model=ScriptedProvider(
            [call("transfer_to_spanish", {"reason": "user spoke Spanish"})]
        ),
        handoffs=[spanish],
    )

    result = await Runner.run(english, "Hola", checkpoint=ckpt(cp, "handoff"))

    assert result.final_agent.name == "Spanish"
    handoff_snapshots = [
        snap
        for snap in cp.saved
        if snap.status == "running" and snap.agent_name == "Spanish"
    ]
    assert handoff_snapshots
    # The snapshot stores the run's own entries (the system prompt is agent-owned
    # and re-rendered on resume, never persisted), and the head records the
    # handoff target — asserted by the ``agent_name == "Spanish"`` filter above.
    first_entry = handoff_snapshots[0].entries[0]
    assert isinstance(first_entry, InputEntry)
    assert first_entry.role == "user"
    assert first_entry.content == "Hola"


@pytest.mark.asyncio
async def test_handoff_preserves_run_level_output_type_contract() -> None:
    class Out(BaseModel):
        value: int

    cp = InMemoryCheckpointer()
    spanish = Agent(
        name="Spanish",
        model=ScriptedProvider([text('{"value": 7}')]),
    )
    english = Agent(
        name="English",
        model=ScriptedProvider(
            [call("transfer_to_spanish", {"reason": "user spoke Spanish"})]
        ),
        handoffs=[spanish],
    )

    await Runner.run(english, "Hola", output_type=Out, checkpoint=ckpt(cp, "handoff"))

    result = await Runner.run(
        spanish,
        [],
        checkpoint=ckpt(cp, "handoff", if_run_exists="resume_only"),
        output_type=Out,
    )
    assert isinstance(result.output, Out)
    assert result.output.value == 7


@pytest.mark.asyncio
async def test_handoff_without_override_uses_target_agent_output_type() -> None:
    class Out(BaseModel):
        value: int

    cp = InMemoryCheckpointer()
    spanish = Agent(
        name="Spanish",
        output_type=Out,
        model=ScriptedProvider([text('{"value": 9}')]),
    )
    english = Agent(
        name="English",
        model=ScriptedProvider(
            [call("transfer_to_spanish", {"reason": "user spoke Spanish"})]
        ),
        handoffs=[spanish],
    )

    result = await Runner.run(english, "Hola", checkpoint=ckpt(cp, "agent-output"))
    snap = await cp.load("agent-output")

    assert isinstance(result.output, Out)
    assert result.output.value == 9
    # The completed snapshot records the *active* agent — the handoff target.
    assert snap is not None and snap.agent_name == "Spanish"


@pytest.mark.asyncio
async def test_resume_only_missing_run_id_raises() -> None:
    cp = InMemoryCheckpointer()
    agent = Agent(name="a", model=ScriptedProvider([]))
    with pytest.raises(Exception, match="No snapshot"):
        await Runner.run(
            agent, [], checkpoint=ckpt(cp, "missing", if_run_exists="resume_only")
        )


@pytest.mark.asyncio
async def test_sqlite_checkpointer_persists_across_instances(tmp_path: Any) -> None:
    db = tmp_path / "ckpt.sqlite"
    cp = SQLiteCheckpointer(db)
    provider = ScriptedProvider([text("persisted")])
    agent = Agent(name="a", model=provider)
    await Runner.run(agent, "hi", checkpoint=ckpt(cp, "r3"))

    cp2 = SQLiteCheckpointer(db)
    snap = await cp2.load("r3")
    assert snap is not None and snap.agent_name == "a"


@pytest.mark.asyncio
async def test_sqlite_checkpointer_delete_is_idempotent(tmp_path: Any) -> None:
    cp = SQLiteCheckpointer(tmp_path / "ckpt.sqlite")
    provider = ScriptedProvider([text("x")])
    agent = Agent(name="a", model=provider)
    await Runner.run(agent, "hi", checkpoint=ckpt(cp, "r"))
    await cp.delete("r")
    await cp.delete("r")  # second delete must not raise
    assert await cp.load("r") is None


def test_snapshot_to_dict_round_trip_preserves_multimodal_content() -> None:
    @dataclass
    class Raw:
        value: int

    snap = RunSnapshot(
        run_id="r",
        agent_name="a",
        entries=[
            InputEntry(
                role="user",
                content=[TextPart("describe this"), ImagePart(url="https://x/y.png")],
            ),
            AssistantTextEntry(content="a cat"),
            ToolResultEntry(call_id="c1", output="raw", raw=Raw(42)),
        ],
        usage=Usage(input_tokens=3, output_tokens=2, cache_read_tokens=1),
        turns=1,
        status="completed",
        output={"ok": True},
        last_input_tokens=3,
    )
    payload = snap.to_dict()
    restored = RunSnapshot.from_dict(payload)
    assert restored.run_id == snap.run_id
    assert restored.status == "completed"
    assert restored.output == {"ok": True}
    assert restored.last_input_tokens == 3
    assert restored.usage.cache_read_tokens == 1
    first = restored.entries[0]
    assert isinstance(first, InputEntry)
    assert isinstance(first.content, list)
    assert isinstance(first.content[0], TextPart)
    assert isinstance(first.content[1], ImagePart)
    assert first.content[1].url == "https://x/y.png"
    raw_entry = restored.entries[2]
    assert isinstance(raw_entry, ToolResultEntry)
    assert raw_entry.raw == {"value": 42}


def test_snapshot_from_dict_defaults_missing_optional_fields() -> None:
    # On-disk snapshots outlive code versions: a payload written before newer
    # optional fields existed must still load, with defaults applied.
    snap = RunSnapshot.from_dict(
        {
            "run_id": "r",
            "entries": [],
            "agent_name": "a",
            "status": "running",
        }
    )
    assert snap.turns == 0
    assert snap.usage.total_tokens == 0
    assert snap.output is None and snap.error is None
    assert snap.last_input_tokens is None
    assert snap.context_state == {}
    assert snap.updated_at > 0
