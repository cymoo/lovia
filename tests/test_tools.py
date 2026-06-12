"""Tests for the flat tool policies (``retries`` / ``timeout`` / ``wrap`` /
``result_renderer``) and RunContext annotation-based injection."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import BaseModel

from lovia import Agent, RunContext, Runner, UserError, tool
from lovia.tools import default_result_renderer, render_tool_result, run_tool

from .scripted_provider import ScriptedProvider, call, text


@pytest.mark.asyncio
async def test_tool_policies_compose_in_order() -> None:
    seen: dict[str, Any] = {}

    async def normalize(invoke, args, ctx):
        return await invoke({"city": args["city"].lower()}, ctx)

    async def tag(invoke, args, ctx):
        return f"tag:{await invoke(args, ctx)}"

    @tool(policies=[tag, normalize])
    async def weather(city: str) -> str:
        seen["city"] = city
        return f"sunny in {city}"

    provider = ScriptedProvider([call("weather", {"city": "PARIS"}), text("done")])
    agent = Agent(name="a", model=provider, tools=[weather])
    result = await Runner.run(agent, "hi")

    assert seen["city"] == "paris"
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert last_tool.content == "tag:sunny in paris"


@pytest.mark.asyncio
async def test_retries_then_success() -> None:
    attempts = {"n": 0}

    @tool(retries=2)
    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("nope")
        return "ok"

    provider = ScriptedProvider([call("flaky", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[flaky])
    result = await Runner.run(agent, "go")
    assert attempts["n"] == 3
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert last_tool.content == "ok"


@pytest.mark.asyncio
async def test_retries_receive_fresh_top_level_args() -> None:
    seen: list[dict[str, Any]] = []
    attempts = {"n": 0}

    async def mutate(invoke, args, ctx):
        seen.append(dict(args))
        args["value"] = "mutated"
        return await invoke(args, ctx)

    @tool(retries=1, policies=[mutate])
    async def flaky(value: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("try again")
        return value

    ctx = RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]
    assert await run_tool(flaky, {"value": "original"}, ctx) == "mutated"
    assert seen == [{"value": "original"}, {"value": "original"}]


@pytest.mark.asyncio
async def test_retries_exhausted_surfaces_as_tool_error() -> None:
    @tool(retries=1)
    async def always_fail() -> str:
        raise RuntimeError("boom")

    provider = ScriptedProvider([call("always_fail", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[always_fail])
    result = await Runner.run(agent, "go")
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert "Tool error" in last_tool.content and "boom" in last_tool.content


@pytest.mark.asyncio
async def test_timeout_triggers_tool_error() -> None:
    @tool(timeout=0.05)
    async def slow() -> str:
        await asyncio.sleep(0.5)
        return "never"

    provider = ScriptedProvider([call("slow", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[slow])
    result = await Runner.run(agent, "go")
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert "Tool error" in last_tool.content


@pytest.mark.asyncio
async def test_result_renderer_controls_string_sent_to_model() -> None:
    @tool(result_renderer=lambda r, ctx: f"<{r['n']}>")
    async def make_obj() -> dict[str, int]:
        return {"n": 42}

    provider = ScriptedProvider([call("make_obj", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[make_obj])
    result = await Runner.run(agent, "go")
    last_tool = next(m for m in reversed(result.messages) if m.role == "tool")
    assert last_tool.content == "<42>"


@pytest.mark.asyncio
async def test_result_renderer_non_string_result_uses_default_rendering() -> None:
    @tool(result_renderer=lambda r, ctx: {"wrapped": r})
    async def make_obj() -> dict[str, int]:
        return {"n": 42}

    ctx = RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]
    rendered = await render_tool_result(make_obj, {"n": 42}, ctx)
    assert json.loads(rendered) == {"wrapped": {"n": 42}}


@pytest.mark.asyncio
async def test_agent_default_tool_retries_apply_when_tool_unset() -> None:
    attempts = {"n": 0}

    @tool  # retries unset → inherit from agent default
    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("nope")
        return "ok"

    provider = ScriptedProvider([call("flaky", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[flaky], default_tool_retries=1)
    await Runner.run(agent, "go")
    assert attempts["n"] == 2


def test_default_result_renderer_handles_common_python_values() -> None:
    class Color(Enum):
        RED = "red"

    @dataclass
    class Payload:
        when: datetime
        tags: tuple[str, ...]

    class Profile(BaseModel):
        created: datetime
        home: Path

    rendered = default_result_renderer(
        {
            Color.RED: {
                "payload": Payload(datetime(2026, 6, 10, 12, 30), ("a", "b")),
                "profile": Profile(
                    created=datetime(2026, 6, 10, 12, 31),
                    home=Path("/tmp/lovia"),
                ),
                "day": date(2026, 6, 10),
                "amount": Decimal("12.50"),
                "id": UUID("12345678-1234-5678-1234-567812345678"),
                "blob": b"hello",
            }
        }
    )

    assert json.loads(rendered) == {
        "red": {
            "payload": {"when": "2026-06-10T12:30:00", "tags": ["a", "b"]},
            "profile": {
                "created": "2026-06-10T12:31:00",
                "home": "/tmp/lovia",
            },
            "day": "2026-06-10",
            "amount": "12.50",
            "id": "12345678-1234-5678-1234-567812345678",
            "blob": "hello",
        }
    }


# ---- RunContext annotation injection (carried over from Phase 1) ----


@dataclass
class _Deps:
    user_id: int


@pytest.mark.asyncio
async def test_run_context_injected_by_annotation() -> None:
    seen: dict[str, Any] = {}

    @tool
    async def whoami(ctx: RunContext[_Deps]) -> str:
        seen["user_id"] = ctx.context.user_id if ctx.context else None
        return "ok"

    provider = ScriptedProvider([call("whoami", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[whoami])
    await Runner.run(agent, "hi", context=_Deps(user_id=42))
    assert seen["user_id"] == 42


def test_tool_rejects_multiple_run_context_parameters() -> None:
    async def bad(ctx1: RunContext[_Deps], ctx2: RunContext[_Deps]) -> str:
        return "bad"

    with pytest.raises(UserError, match="at most one RunContext"):
        tool(bad)


def test_tool_respects_explicit_empty_description() -> None:
    @tool(description="")
    async def documented() -> str:
        """This docstring should not be used."""
        return "ok"

    assert documented.description == ""


@pytest.mark.asyncio
async def test_param_named_ctx_without_annotation_is_a_regular_arg() -> None:
    """Bare ``ctx: str`` (no RunContext annotation) is a normal LLM-supplied arg."""

    captured: dict[str, Any] = {}

    @tool
    async def echo(ctx: str) -> str:
        captured["ctx"] = ctx
        return ctx

    provider = ScriptedProvider([call("echo", {"ctx": "hello"}), text("done")])
    agent = Agent(name="a", model=provider, tools=[echo])
    await Runner.run(agent, "hi")
    assert captured["ctx"] == "hello"


# ---------------------------------------------------------------------------
# Tool-output caps (memory bound at the transcript boundary)
# ---------------------------------------------------------------------------


def test_truncate_tool_output_helper() -> None:
    from lovia.tools import truncate_tool_output

    assert truncate_tool_output("short", 100) == "short"
    text_in = "H" * 8_000 + "T" * 2_000
    out = truncate_tool_output(text_in, 1_000)
    assert out.startswith("H" * 800)
    assert out.endswith("T" * 200)
    assert "truncated: kept 1,000 of 10,000 chars" in out
    assert len(out) < 1_200  # limit plus the marker


async def test_agent_level_output_cap_truncates_and_drops_raw() -> None:
    from lovia.transcript import ToolResultEntry

    @tool
    def big() -> str:
        """Return a huge payload."""
        return "x" * 50_000

    provider = ScriptedProvider([call("big", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[big], max_tool_output_chars=2_000)
    result = await Runner.run(agent, "go")

    entry = next(e for e in result.entries if isinstance(e, ToolResultEntry))
    assert len(entry.output) < 2_200
    assert "truncated" in entry.output
    assert entry.raw is None  # the giant raw value is not retained
    # The model saw the truncated version too.
    tool_msg = next(m for m in provider.calls[1] if m.role == "tool")
    assert "truncated" in tool_msg.content


async def test_per_tool_cap_overrides_agent_default() -> None:
    from lovia.transcript import ToolResultEntry

    @tool(max_output_chars=500)
    def big() -> str:
        """Return a huge payload."""
        return "y" * 50_000

    provider = ScriptedProvider([call("big", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[big], max_tool_output_chars=100_000)
    result = await Runner.run(agent, "go")
    entry = next(e for e in result.entries if isinstance(e, ToolResultEntry))
    assert len(entry.output) < 700


async def test_no_cap_keeps_full_output_and_raw() -> None:
    from lovia.transcript import ToolResultEntry

    @tool
    def big() -> dict:
        """Return a structured payload."""
        return {"data": "z" * 10_000}

    provider = ScriptedProvider([call("big", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[big])
    result = await Runner.run(agent, "go")
    entry = next(e for e in result.entries if isinstance(e, ToolResultEntry))
    assert "z" * 10_000 in entry.output
    assert entry.raw == {"data": "z" * 10_000}


async def test_small_output_not_touched_by_cap() -> None:
    from lovia.transcript import ToolResultEntry

    @tool
    def small() -> str:
        """Return a small payload."""
        return "tiny result"

    provider = ScriptedProvider([call("small", {}), text("done")])
    agent = Agent(name="a", model=provider, tools=[small], max_tool_output_chars=2_000)
    result = await Runner.run(agent, "go")
    entry = next(e for e in result.entries if isinstance(e, ToolResultEntry))
    assert entry.output == "tiny result"
    assert entry.raw == "tiny result"
