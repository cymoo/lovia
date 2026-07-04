from __future__ import annotations

import pytest

from lovia import Agent, CancelToken, RunCancelled, RunContext, Runner, tool

from .scripted_provider import ScriptedProvider, call, text


async def test_handoff_switches_agents() -> None:
    spanish = Agent(
        name="Spanish",
        instructions="reply in Spanish",
        model=ScriptedProvider([text("Hola!")]),
    )
    # The English agent first hands off, then never speaks again because the
    # Spanish provider takes over.
    english = Agent(
        name="English",
        instructions="reply in English",
        model=ScriptedProvider(
            [call("transfer_to_spanish", {"reason": "user spoke Spanish"})]
        ),
        handoffs=[spanish],
    )
    result = await Runner.run(english, "Hola")
    assert result.final_agent.name == "Spanish"
    assert result.output == "Hola!"


async def test_agent_as_tool() -> None:
    expert = Agent(
        name="Expert",
        model=ScriptedProvider([text("42 is the answer")]),
    )
    # The parent calls the expert via a tool, then formats the reply.
    parent_provider = ScriptedProvider(
        [
            call("ask_expert", {"input": "what is the answer?"}, call_id="c1"),
            text("Got it: 42"),
        ]
    )
    parent = Agent(
        name="Parent",
        model=parent_provider,
        tools=[expert.as_tool()],
    )
    result = await Runner.run(parent, "delegate to expert")
    assert result.output == "Got it: 42"


async def test_agent_as_tool_forwards_max_turns() -> None:
    # The expert keeps calling a (missing) tool; only on its third turn would
    # it produce "done". max_turns=1 caps the sub-run before then, so the
    # sub-run raises MaxTurnsExceeded. The parent catches that as a tool error
    # and continues to its own next message.
    expert = Agent(
        name="Expert",
        model=ScriptedProvider([call("noop", {}), call("noop", {}), text("done")]),
    )
    tool = expert.as_tool(max_turns=1)
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [call(tool.name, {"input": "go"}, call_id="c1"), text("ok")]
        ),
        tools=[tool],
    )
    result = await Runner.run(parent, "delegate")
    # The expert's "done" is never reached; the parent recovers and answers.
    assert result.output == "ok"


async def test_agent_as_tool_inherits_cancel_token() -> None:
    # The sub-run must see the *same* token instance the parent run was given,
    # so a cancel() can reach it while the parent is blocked awaiting the child.
    token = CancelToken()
    seen: list[CancelToken] = []

    @tool
    async def record(ctx: RunContext) -> str:
        seen.append(ctx.cancel_token)
        return "noted"

    child = Agent(
        name="Child",
        model=ScriptedProvider([call("record", {}), text("child done")]),
        tools=[record],
    )
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [call("ask_child", {"input": "go"}, call_id="c1"), text("ok")]
        ),
        tools=[child.as_tool()],
    )
    result = await Runner.run(parent, "delegate", cancel_token=token)
    assert result.output == "ok"
    assert seen == [token]  # the child's tool saw the parent's exact token


async def test_cancel_inside_sub_run_terminates_parent() -> None:
    # A cancel issued from within the sub-run (via the inherited token) must
    # propagate up and terminate the parent run — not be swallowed into a
    # tool-error result the way an arbitrary tool exception would be.
    @tool
    async def stop(ctx: RunContext) -> str:
        ctx.cancel_token.cancel("child decided to stop")
        return "stopping"

    child = Agent(
        name="Child",
        model=ScriptedProvider([call("stop", {}), text("never reached")]),
        tools=[stop],
    )
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [call("ask_child", {"input": "go"}, call_id="c1"), text("never reached")]
        ),
        tools=[child.as_tool()],
    )
    with pytest.raises(RunCancelled):
        await Runner.run(parent, "delegate")


async def test_second_handoff_in_same_turn_is_rejected_unrun() -> None:
    # The first handoff of a turn wins. A second transfer requested in the
    # same turn must be rejected *before* it runs: its on_handoff side effects
    # never fire and its tool result says so instead of claiming a transfer.
    import json as _json

    from lovia.handoff import Handoff
    from lovia.messages import AssistantTurn, ToolCall, Usage
    from lovia.transcript import ToolResultEntry

    fired: list[str] = []
    alpha = Agent(name="alpha", model=ScriptedProvider([text("from alpha")]))
    beta = Agent(name="beta", model=ScriptedProvider([text("from beta")]))

    double_transfer = AssistantTurn(
        content=None,
        tool_calls=[
            ToolCall(id="h1", name="transfer_to_alpha", arguments=_json.dumps({})),
            ToolCall(id="h2", name="transfer_to_beta", arguments=_json.dumps({})),
        ],
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    triage = Agent(
        name="triage",
        model=ScriptedProvider([double_transfer]),
        handoffs=[
            Handoff(target=alpha, on_handoff=lambda a, c: fired.append("alpha")),
            Handoff(target=beta, on_handoff=lambda a, c: fired.append("beta")),
        ],
    )

    result = await Runner.run(triage, "route me")

    assert result.final_agent.name == "alpha"
    assert result.output == "from alpha"
    assert fired == ["alpha"]  # beta's callback never ran
    results = {e.call_id: e for e in result.entries if isinstance(e, ToolResultEntry)}
    assert "Transferred to alpha" in results["h1"].output
    assert results["h2"].is_error
    assert "already transferred to 'alpha'" in results["h2"].output


async def test_agent_as_tool_budget_is_per_invocation() -> None:
    # RunBudget carries internal state (its clock start, the tool-call count),
    # so the instance given to as_tool() must be copied per invocation: two
    # sub-runs that each fit the budget individually must both succeed.
    from lovia import RunBudget
    from lovia.transcript import ToolResultEntry

    @tool
    async def ping() -> str:
        return "pong"

    def child_turns():
        return [
            call("ping", {}, call_id="p1"),
            call("ping", {}, call_id="p2"),
            text("child done"),
        ]

    child = Agent(
        name="Child",
        model=ScriptedProvider(child_turns() + child_turns()),
        tools=[ping],
    )
    # One sub-run makes 2 tool calls; a shared budget would start the second
    # sub-run with the counter already at 2 and trip at its second call.
    budget = RunBudget(max_tool_calls=3)
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [
                call("ask_child", {"input": "first"}, call_id="c1"),
                call("ask_child", {"input": "second"}, call_id="c2"),
                text("both fine"),
            ]
        ),
        tools=[child.as_tool(budget=budget)],
    )

    result = await Runner.run(parent, "delegate twice")

    assert result.output == "both fine"
    child_results = [
        e.output
        for e in result.entries
        if isinstance(e, ToolResultEntry) and e.call_id in ("c1", "c2")
    ]
    assert child_results == ["child done", "child done"]


async def test_agent_as_tool_child_budget_exhaustion_is_a_tool_error() -> None:
    # Budgets are scoped: a sub-run blowing ITS OWN budget is a recoverable
    # delegation failure — the parent sees a tool error and continues, exactly
    # like the sub-run's MaxTurnsExceeded (and unlike RunCancelled, which is
    # run-global and terminates the parent).
    from lovia import RunBudget
    from lovia.transcript import ToolResultEntry

    child = Agent(name="Child", model=ScriptedProvider([text("hi")]))
    # The child's single model call uses 2 tokens; a 1-token budget trips
    # right after it.
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [call("ask_child", {"input": "go"}, call_id="c1"), text("recovered")]
        ),
        tools=[child.as_tool(budget=RunBudget(max_total_tokens=1))],
    )

    result = await Runner.run(parent, "delegate")

    assert result.output == "recovered"
    [child_result] = [
        e
        for e in result.entries
        if isinstance(e, ToolResultEntry) and e.call_id == "c1"
    ]
    assert child_result.is_error
    assert "exceeds budget" in child_result.output


def test_slug_produces_provider_legal_ascii_tool_names() -> None:
    from lovia.handoff import _slug, build_handoff_tool, Handoff

    assert _slug("Route Finder") == "route_finder"
    assert _slug("café") == "caf"  # non-ASCII dropped, not passed through
    assert _slug("") == "agent"

    # Fully non-ASCII names fall back to a stable digest: still ASCII, and
    # distinct agents get distinct names.
    cn_support, cn_sales = _slug("客服"), _slug("销售")
    assert cn_support.isascii() and cn_support.startswith("agent_")
    assert cn_support != cn_sales
    assert _slug("客服") == cn_support  # stable across calls

    tool_name = build_handoff_tool(
        Handoff(target=Agent(name="客服", model=ScriptedProvider([])))
    ).name
    assert tool_name.isascii()
    assert tool_name.startswith("transfer_to_agent_")


def test_generated_tool_names_fit_provider_length_cap() -> None:
    from lovia.handoff import Handoff, agent_as_tool, build_handoff_tool

    long_a = "very_long_agent_name_" + "a" * 60
    long_b = "very_long_agent_name_" + "b" * 60

    name_a = build_handoff_tool(Handoff(target=Agent(name=long_a))).name
    name_b = build_handoff_tool(Handoff(target=Agent(name=long_b))).name
    assert len(name_a) <= 64 and len(name_b) <= 64
    assert name_a.startswith("transfer_to_very_long_agent_name_")
    assert name_a != name_b  # digest suffix keeps distinct agents distinct
    # Stable across calls, like the digest fallback above.
    assert name_a == build_handoff_tool(Handoff(target=Agent(name=long_a))).name

    ask_name = agent_as_tool(Agent(name=long_a)).name
    assert len(ask_name) <= 64 and ask_name.startswith("ask_very_long")


async def test_agent_as_tool_inherits_tracer() -> None:
    # The sub-run inherits the parent's tracer (internal RunContext plumbing)
    # so its spans join the parent's trace instead of vanishing into a
    # NoopTracer.
    from lovia.tracing import NoopTracer

    tracer = NoopTracer()
    seen: list[object] = []

    @tool
    async def record_tracer(ctx: RunContext) -> str:
        seen.append(ctx._tracer)
        return "noted"

    child = Agent(
        name="Child",
        model=ScriptedProvider([call("record_tracer", {}), text("child done")]),
        tools=[record_tracer],
    )
    parent = Agent(
        name="Parent",
        model=ScriptedProvider(
            [call("ask_child", {"input": "go"}, call_id="c1"), text("ok")]
        ),
        tools=[child.as_tool()],
    )
    result = await Runner.run(parent, "delegate", tracer=tracer)
    assert result.output == "ok"
    assert seen == [tracer]  # the child's tool saw the parent's exact tracer
