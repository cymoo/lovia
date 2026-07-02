"""Unit tests for ``lovia.runtime.resume`` — handoff-graph walk and the
rebuild of a completed snapshot's :class:`RunResult`.
"""

from __future__ import annotations

import pytest

from lovia import Agent
from lovia.checkpointer import RunSnapshot
from lovia.exceptions import UserError
from lovia.handoff import Handoff
from lovia.messages import Usage
from lovia.runtime.resume import (
    reachable_agents,
    resolve_resume_agent,
    result_from_completed_snapshot,
)


def _snapshot(agent_name: str, **kw) -> RunSnapshot:
    return RunSnapshot(
        run_id="r1",
        agent_name=agent_name,
        entries=kw.pop("entries", []),
        usage=kw.pop("usage", Usage()),
        turns=kw.pop("turns", 1),
        **kw,
    )


# --------------------------------------------------------- reachable_agents


def test_reachable_includes_entry_alone() -> None:
    a = Agent(name="solo")
    assert reachable_agents(a) == {"solo": a}


def test_reachable_follows_handoffs_transitively() -> None:
    c = Agent(name="c")
    b = Agent(name="b", handoffs=[c])
    a = Agent(name="a", handoffs=[b])
    assert set(reachable_agents(a)) == {"a", "b", "c"}


def test_reachable_handles_cycles() -> None:
    # a <-> b mutual handoff must not loop forever (the in-``found`` guard).
    a = Agent(name="a")
    b = Agent(name="b", handoffs=[a])
    a.handoffs = [b]
    assert set(reachable_agents(a)) == {"a", "b"}


def test_reachable_unwraps_handoff_objects() -> None:
    b = Agent(name="b")
    a = Agent(name="a", handoffs=[Handoff(target=b, description="go to b")])
    assert set(reachable_agents(a)) == {"a", "b"}


# ------------------------------------------------------ resolve_resume_agent


def test_resolve_returns_the_recorded_active_agent() -> None:
    b = Agent(name="b")
    a = Agent(name="a", handoffs=[b])
    assert resolve_resume_agent(a, _snapshot("b")) is b


def test_resolve_raises_when_active_agent_unreachable() -> None:
    a = Agent(name="a")
    with pytest.raises(UserError, match="not reachable"):
        resolve_resume_agent(a, _snapshot("ghost"))


# ------------------------------------------------ result_from_completed_snapshot


def test_rebuild_str_output_passthrough() -> None:
    a = Agent(name="a")
    res = result_from_completed_snapshot(
        a, _snapshot("a", output="hello"), output_type=str
    )
    assert res.output == "hello"
    assert res.final_agent is a


def test_rebuild_str_output_defaults_none_to_empty() -> None:
    a = Agent(name="a")
    res = result_from_completed_snapshot(
        a, _snapshot("a", output=None), output_type=str
    )
    assert res.output == ""


def test_rebuild_coerces_non_str_output() -> None:
    a = Agent(name="a")
    res = result_from_completed_snapshot(a, _snapshot("a", output=5), output_type=int)
    assert res.output == 5


def test_rebuild_rejects_non_serializable_completed_output() -> None:
    a = Agent(name="a")
    snap = _snapshot("a", output=None, error={"type": "OutputNotSerializable"})
    with pytest.raises(UserError, match="not JSON-safe"):
        result_from_completed_snapshot(a, snap, output_type=str)
