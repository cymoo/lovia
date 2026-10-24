"""History timestamps and answer durations from ``Segment.meta["timing"]``."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.session import TIMING_META_KEY, Segment  # noqa: E402
from lovia.transcript import (  # noqa: E402
    AssistantTextEntry,
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
)
from lovia.web import ChatStore, create_app  # noqa: E402
from lovia.web.api.serialization import segments_to_out  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402


def _timing(start: float, finish: float) -> dict:
    return {TIMING_META_KEY: {"started_at": start, "finished_at": finish}}


def _exchange(question: str, answer: str) -> list:
    return [
        InputEntry(role="user", content=question),
        AssistantTextEntry(content=answer),
    ]


def test_a_timed_run_spans_its_start_to_its_finish() -> None:
    entries = [
        InputEntry(role="user", content="go"),
        ToolCallEntry(call_id="c1", name="look", arguments="{}"),
        ToolResultEntry(call_id="c1", output="seen"),
        AssistantTextEntry(content="done"),
    ]
    outs = segments_to_out(
        [Segment("r1", entries, _timing(100.0, 160.0))], created_at=0, updated_at=999
    )
    assert [o.role for o in outs] == ["user", "assistant", "tool", "assistant"]
    assert [o.timestamp for o in outs] == [100.0, 120.0, 140.0, 160.0]
    # Only the answer that ends the run says how long it took.
    assert [o.duration for o in outs] == [None, None, None, 60.0]


def test_untimed_runs_spread_between_their_known_neighbours() -> None:
    segments = [
        Segment("old", _exchange("a", "b")),  # from before timing was recorded
        Segment("r1", _exchange("c", "d"), _timing(100.0, 110.0)),
        Segment("r2", _exchange("e", "f")),
    ]
    outs = segments_to_out(segments, created_at=40.0, updated_at=170.0)
    assert [o.timestamp for o in outs] == [40.0, 70.0, 100.0, 110.0, 140.0, 170.0]
    assert [o.duration for o in outs] == [None, None, None, 10.0, None, None]


def test_a_stopped_run_ending_on_a_tool_result_finishes_on_its_answer() -> None:
    # A user stop can leave the segment ending on a tool result; the answer
    # carrying the duration is still the one stamped with the finish.
    entries = [
        InputEntry(role="user", content="go"),
        ToolCallEntry(call_id="c1", name="look", arguments="{}"),
        ToolResultEntry(call_id="c1", output="seen"),
    ]
    outs = segments_to_out(
        [Segment("r1", entries, _timing(100.0, 160.0))], created_at=0, updated_at=999
    )
    assert [(o.role, o.timestamp, o.duration) for o in outs] == [
        ("user", 100.0, None),
        ("assistant", 160.0, 60.0),
        ("tool", 160.0, None),
    ]


def test_gaps_stay_in_order_whatever_the_bounds() -> None:
    # A bound past the first timed run (a view built with "now" as the chat's
    # start) must not put older messages after newer ones.
    segments = [
        Segment("old", _exchange("a", "b")),
        Segment("r1", _exchange("c", "d"), _timing(100.0, 110.0)),
    ]
    stamps = [
        o.timestamp for o in segments_to_out(segments, created_at=500, updated_at=500)
    ]
    assert stamps == sorted(stamps)


def test_with_no_timing_the_spread_is_as_before() -> None:
    segments = [
        Segment("a", _exchange("q1", "a1")),
        Segment("b", _exchange("q2", "a2")),
    ]
    outs = segments_to_out(segments, created_at=0.0, updated_at=30.0)
    assert [o.timestamp for o in outs] == [0.0, 10.0, 20.0, 30.0]
    assert all(o.duration is None for o in outs)


async def test_a_resume_from_before_timing_sends_no_timing() -> None:
    # Nothing recorded the run's start, so nothing claims a duration — live as
    # on reload, where its segment is untimed.
    import httpx

    from lovia.checkpointer import RunHead

    from .test_web import _parse_sse

    store = ChatStore.in_memory()
    await store.upsert("s", agent="bot")
    head = RunHead.from_dict({"agent_name": "bot", "status": "interrupted", "turns": 0})
    await store.checkpointer.append("r1", [InputEntry(role="user", content="go")], head)
    await store.set_active_run_id("s", "r1")
    app = create_app(
        Agent(name="bot", model=ScriptedProvider([text("hi")])),
        store=store,
        titles=False,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with ac.stream(
            "POST", "/api/chat/reconnect", params={"session_id": "s"}
        ) as res:
            lines = [line async for line in res.aiter_lines()]
    kinds = [e for e, _ in _parse_sse("\n".join(lines))]
    assert kinds[-1] == "done" and "timing" not in kinds
    (seg,) = await store.session.segments("s")
    assert TIMING_META_KEY not in (seg.meta or {})


def test_session_detail_carries_the_duration() -> None:
    import asyncio

    store = ChatStore.in_memory()
    asyncio.run(store.upsert("s", agent="bot"))
    asyncio.run(
        store.session.append(
            "s", _exchange("hi", "hello"), run_id="r1", meta=_timing(100.0, 112.5)
        )
    )
    app = create_app(
        Agent(name="bot", model=ScriptedProvider([])), store=store, titles=False
    )
    entries = TestClient(app).get("/api/sessions/s").json()["entries"]
    assert [(e["timestamp"], e["duration"]) for e in entries] == [
        (100.0, None),
        (112.5, 12.5),
    ]
