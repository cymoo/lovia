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

from ..scripted_provider import ScriptedProvider  # noqa: E402


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


def test_with_no_timing_the_spread_is_as_before() -> None:
    segments = [
        Segment("a", _exchange("q1", "a1")),
        Segment("b", _exchange("q2", "a2")),
    ]
    outs = segments_to_out(segments, created_at=0.0, updated_at=30.0)
    assert [o.timestamp for o in outs] == [0.0, 10.0, 20.0, 30.0]
    assert all(o.duration is None for o in outs)


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
