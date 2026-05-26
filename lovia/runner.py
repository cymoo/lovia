"""The runtime that drives an :class:`Agent` to completion.

This is the only place in the framework that touches mutable state. It
orchestrates:

* Building the message list from instructions, optional session history,
  optional skill catalog, and the user input.
* Calling the provider in a loop, parsing tool calls, dispatching them, and
  feeding results back into the conversation.
* Handling structured output, multi-agent handoffs, human approval, and
  event hooks.

Public surface area is small: :meth:`Runner.run` and :meth:`Runner.run_stream`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Generic, TypeVar

from . import events
from .agent import Agent
from .exceptions import ApprovalDenied, MaxTurnsExceeded, UserError
from .handoff import Handoff, _HandoffSignal, build_handoff_tool
from .hooks import dispatch
from .messages import AssistantMessage, ChatMessage, ToolCall, Usage, system, tool_message, user
from .output import (
    FINAL_OUTPUT_TOOL_NAME,
    OutputSpec,
    build_output_spec,
    final_output_tool_schema,
    loads_lenient,
    parse_output,
    response_format_for,
)
from .providers import Provider
from .providers.openai_chat import OpenAIChatProvider
from .session import Session
from .tools import Tool


TContext = TypeVar("TContext")
TOutput = TypeVar("TOutput")


@dataclass
class RunContext(Generic[TContext]):
    """The per-run state passed to tools and hooks.

    Tools that declare a ``ctx`` (or ``context``) parameter receive this
    object. ``context`` is whatever opaque value the caller passed to
    :meth:`Runner.run`; ``messages`` is the live, mutable transcript.
    """

    context: TContext | None
    messages: list[ChatMessage]
    agent: Agent
    usage: Usage = field(default_factory=Usage)
    # Per-tool-call approval decisions made by the caller while streaming.
    # The key is the tool call id; the value is True/False.
    approvals: dict[str, bool] = field(default_factory=dict)


@dataclass
class RunResult(Generic[TOutput]):
    """The terminal state of a completed run."""

    output: TOutput
    messages: list[ChatMessage]
    final_agent: Agent
    usage: Usage
    turns: int


class Runner:
    """Stateless orchestrator. All entry points are class/static methods."""

    @staticmethod
    async def run(
        agent: Agent,
        input: "str | list[ChatMessage]",
        *,
        context: Any = None,
        session: Session | None = None,
        session_id: str | None = None,
        max_turns: int = 20,
    ) -> RunResult[Any]:
        """Run ``agent`` to completion and return the final result."""
        # Consume the stream internally; ``run`` is just a convenience over it.
        last_result: RunResult[Any] | None = None
        async for event in Runner.run_stream(
            agent,
            input,
            context=context,
            session=session,
            session_id=session_id,
            max_turns=max_turns,
        ):
            if isinstance(event, events.RunCompleted):
                last_result = event.result
        assert last_result is not None, "Runner.run_stream did not emit RunCompleted"
        return last_result

    @staticmethod
    async def run_stream(
        agent: Agent,
        input: "str | list[ChatMessage]",
        *,
        context: Any = None,
        session: Session | None = None,
        session_id: str | None = None,
        max_turns: int = 20,
    ) -> AsyncIterator[events.Event]:
        """Run ``agent`` and yield :class:`Event` instances as they happen."""
        async for event in _RunLoop(
            initial_agent=agent,
            user_input=input,
            context=context,
            session=session,
            session_id=session_id,
            max_turns=max_turns,
        ).stream():
            yield event


class _RunLoop:
    """The actual event-producing async iterator.

    Kept as a class (rather than a long async generator) because it carries a
    small amount of mutable state across turns: the active agent, the
    transcript, accumulated usage, and the resolved output spec.
    """

    def __init__(
        self,
        *,
        initial_agent: Agent,
        user_input: "str | list[ChatMessage]",
        context: Any,
        session: Session | None,
        session_id: str | None,
        max_turns: int,
    ) -> None:
        if session is not None and session_id is None:
            raise UserError("session_id is required when session is provided")
        self.agent = initial_agent
        self.user_input = user_input
        self.context = context
        self.session = session
        self.session_id = session_id
        self.max_turns = max_turns

    async def stream(self) -> AsyncIterator[events.Event]:
        agent = self.agent

        # 1. Build the initial conversation: system prompt + (session history) + input.
        transcript = await self._build_initial_messages(agent)
        run_ctx = RunContext(context=self.context, messages=transcript, agent=agent)

        # 2. Discover MCP tools (if any). Connections are kept open for the
        # whole run and closed in a finally block.
        mcp_tools, mcp_cleanup = await self._connect_mcp(agent)

        # 3. Resolve output strategy and base tool list.
        output_spec = build_output_spec(agent.output_type, _supports_json_schema(agent))
        tools_by_name = self._collect_tools(agent, mcp_tools, output_spec)

        ev_start = events.RunStarted(agent=agent)
        yield ev_start
        await dispatch(agent.hooks, ev_start)
        try:
            output: Any = None
            turns = 0
            while True:
                if turns >= self.max_turns:
                    raise MaxTurnsExceeded(
                        f"Run exceeded max_turns={self.max_turns} without producing output"
                    )
                turns += 1
                ev_turn = events.TurnStarted(agent=agent, turn=turns)
                yield ev_turn
                await dispatch(agent.hooks, ev_turn)

                provider = agent.resolve_provider()
                assistant = None
                async for chunk in provider.stream(
                    transcript,
                    tools=[t.openai_schema() for t in tools_by_name.values()] or None,
                    response_format=(
                        response_format_for(output_spec)
                        if output_spec and not output_spec.use_tool_fallback
                        else None
                    ),
                    settings=agent.settings,
                ):
                    if chunk.text_delta is not None:
                        yield events.TextDelta(delta=chunk.text_delta)
                        await dispatch(agent.hooks, events.TextDelta(delta=chunk.text_delta))
                    if chunk.done is not None:
                        assistant = chunk.done

                if assistant is None:
                    # Provider exited without emitting ``done`` - shouldn't
                    # happen for well-behaved adapters, but be defensive.
                    raise RuntimeError("Provider stream ended without final message")

                run_ctx.usage.add(assistant.usage)
                msg = assistant.to_chat_message()
                transcript.append(msg)
                ev_msg = events.MessageCompleted(message=msg)
                yield ev_msg
                await dispatch(agent.hooks, ev_msg)

                # No tool calls -> we're done. Parse text or JSON output.
                if not assistant.tool_calls:
                    output = await self._finalize_text_output(assistant, output_spec)
                    ev_end = events.TurnEnded(agent=agent, turn=turns)
                    yield ev_end
                    await dispatch(agent.hooks, ev_end)
                    break

                # Process tool calls. May trigger a handoff, in which case we
                # swap ``agent`` and continue the loop.
                handoff_target: Agent | None = None
                final_via_tool: Any = None
                for call in assistant.tool_calls:
                    if call.name == FINAL_OUTPUT_TOOL_NAME and output_spec is not None:
                        # Synthetic final-output tool: parse, ack, terminate.
                        final_via_tool = parse_output(output_spec, call.arguments)
                        transcript.append(
                            tool_message(call.id, "ok")
                        )
                        continue

                    tool = tools_by_name.get(call.name)
                    if tool is None:
                        err = f"Tool {call.name!r} is not available."
                        transcript.append(tool_message(call.id, err))
                        yield events.ToolCallCompleted(call=call, result=err, is_error=True)
                        await dispatch(
                            agent.hooks,
                            events.ToolCallCompleted(call=call, result=err, is_error=True),
                        )
                        continue

                    try:
                        args = json.loads(call.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}

                    # Approval gate.
                    if tool.requires_approval(args, run_ctx):
                        ev = events.ApprovalRequired(call=call)
                        yield ev
                        await dispatch(agent.hooks, ev)
                        approved = run_ctx.approvals.get(call.id, ev.approved)
                        if approved is None:
                            # Default to deny if the caller didn't decide.
                            approved = False
                        if not approved:
                            denial = f"Tool {call.name} was not approved."
                            transcript.append(tool_message(call.id, denial))
                            yield events.ToolCallCompleted(
                                call=call, result=denial, is_error=True
                            )
                            await dispatch(
                                agent.hooks,
                                events.ToolCallCompleted(
                                    call=call, result=denial, is_error=True
                                ),
                            )
                            continue

                    yield events.ToolCallStarted(call=call)
                    await dispatch(agent.hooks, events.ToolCallStarted(call=call))

                    try:
                        result = await tool.invoke(args, run_ctx)
                        is_error = False
                    except Exception as exc:
                        result = f"Tool error: {exc}"
                        is_error = True
                        await dispatch(agent.hooks, events.ErrorOccurred(error=exc))

                    if isinstance(result, _HandoffSignal):
                        # Defer the actual swap until after we've recorded the
                        # tool result, so the transcript stays consistent.
                        handoff_target = result.target
                        result_text = (
                            f"Transferred to {result.target.name}"
                            + (f" ({result.reason})" if result.reason else "")
                        )
                    else:
                        result_text = _stringify_tool_result(result)

                    transcript.append(tool_message(call.id, result_text))
                    yield events.ToolCallCompleted(
                        call=call, result=result, is_error=is_error
                    )
                    await dispatch(
                        agent.hooks,
                        events.ToolCallCompleted(call=call, result=result, is_error=is_error),
                    )

                ev_te = events.TurnEnded(agent=agent, turn=turns)
                yield ev_te
                await dispatch(agent.hooks, ev_te)

                if final_via_tool is not None:
                    output = final_via_tool
                    break

                if handoff_target is not None:
                    prev_agent = agent
                    agent = handoff_target
                    run_ctx.agent = agent
                    output_spec = build_output_spec(
                        agent.output_type, _supports_json_schema(agent)
                    )
                    tools_by_name = self._collect_tools(agent, mcp_tools, output_spec)
                    # Update system prompt for the new agent.
                    transcript[:] = await self._reset_system_prompt(transcript, agent)
                    ev = events.HandoffOccurred(from_agent=prev_agent, to_agent=agent)
                    yield ev
                    await dispatch(agent.hooks, ev)

            # Final bookkeeping.
            if output_spec is not None and output is None:
                # Should not happen, but be explicit.
                raise UserError(
                    f"Agent {agent.name!r} ended without producing structured output"
                )

            result = RunResult(
                output=output,
                messages=transcript,
                final_agent=agent,
                usage=run_ctx.usage,
                turns=turns,
            )

            if self.session is not None:
                await self._persist_session(transcript)

            done = events.RunCompleted(result=result)
            yield done
            await dispatch(agent.hooks, done)

        finally:
            for cleanup in mcp_cleanup:
                try:
                    await cleanup()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

    # ------------------------------------------------------------------ helpers

    async def _build_initial_messages(self, agent: Agent) -> list[ChatMessage]:
        msgs: list[ChatMessage] = []
        system_text = await self._system_prompt(agent)
        if system_text:
            msgs.append(system(system_text))

        if self.session is not None:
            history = await self.session.load(self.session_id)  # type: ignore[arg-type]
            msgs.extend(history)

        if isinstance(self.user_input, str):
            msgs.append(user(self.user_input))
        else:
            msgs.extend(self.user_input)
        return msgs

    async def _system_prompt(self, agent: Agent) -> str:
        text = await agent.render_instructions(self.context)
        if agent.skills is not None:
            text = f"{text}\n\n{agent.skills.render_catalog()}".strip()
        return text

    async def _reset_system_prompt(
        self, transcript: list[ChatMessage], agent: Agent
    ) -> list[ChatMessage]:
        """Swap the leading system message when an agent handoff occurs."""
        new_system = await self._system_prompt(agent)
        # Drop any leading system messages; keep the rest.
        body = [m for m in transcript if m.role != "system" or m is not transcript[0]]
        if new_system:
            return [system(new_system), *body]
        return body

    def _collect_tools(
        self,
        agent: Agent,
        mcp_tools: list[Tool],
        output_spec: OutputSpec | None,
    ) -> dict[str, Tool]:
        tools: dict[str, Tool] = {}
        for t in agent.tools:
            tools[t.name] = t
        for t in mcp_tools:
            tools[t.name] = t
        for h in agent.handoffs:
            handoff_obj = h if isinstance(h, Handoff) else Handoff(target=h)
            tool = build_handoff_tool(handoff_obj)
            tools[tool.name] = tool
        if agent.skills is not None:
            for t in agent.skills.tools():
                tools[t.name] = t
        if output_spec is not None and output_spec.use_tool_fallback:
            # Insert the synthetic ``final_output`` tool. Note we don't register
            # it as a real :class:`Tool` because the runner intercepts the call
            # by name; we only need its schema to be advertised to the model.
            tools[FINAL_OUTPUT_TOOL_NAME] = Tool(
                name=FINAL_OUTPUT_TOOL_NAME,
                description="Call once with the final answer.",
                parameters=output_spec.schema,
                invoke=_unreachable_invoke,
            )
        return tools

    async def _connect_mcp(self, agent: Agent) -> tuple[list[Tool], list[Any]]:
        tools: list[Tool] = []
        cleanup: list[Any] = []
        for server in agent.mcp_servers:
            server_tools = await server.connect()
            tools.extend(server_tools)
            cleanup.append(server.aclose)
        return tools, cleanup

    async def _finalize_text_output(
        self,
        assistant: AssistantMessage,
        output_spec: OutputSpec | None,
    ) -> Any:
        if output_spec is None:
            return assistant.content or ""
        # ``response_format`` path: the assistant's content is a JSON document.
        if not output_spec.use_tool_fallback:
            return parse_output(output_spec, loads_lenient(assistant.content or ""))
        # Falling here means the model failed to call ``final_output``. Best
        # effort: try to parse content as JSON anyway.
        return parse_output(output_spec, loads_lenient(assistant.content or ""))

    async def _persist_session(self, transcript: list[ChatMessage]) -> None:
        # Replace stored transcript with the latest (simple and predictable).
        # System prompts are agent-owned and re-rendered each run, so they are
        # excluded from the persisted history.
        assert self.session is not None and self.session_id is not None
        body = [m for m in transcript if m.role != "system"]
        await self.session.clear(self.session_id)
        await self.session.append(self.session_id, body)

    async def _emit(self, event: events.Event, agent: Agent) -> None:
        # Hooks-only dispatch. Used for events the loop itself yields elsewhere.
        await dispatch(agent.hooks, event)


def _supports_json_schema(agent: Agent) -> bool:
    """Whether the agent's provider can use OpenAI-style ``response_format``."""
    provider = agent.resolve_provider() if isinstance(agent.model, str) else agent.model
    return isinstance(provider, OpenAIChatProvider)


def _stringify_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, ensure_ascii=False)
    except TypeError:
        return str(result)


async def _unreachable_invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
    raise AssertionError("final_output tool must be intercepted by the runner")


# Re-export for convenience.
__all__ = ["Runner", "RunContext", "RunResult"]
