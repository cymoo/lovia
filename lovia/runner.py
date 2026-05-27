"""The runtime that drives an :class:`Agent` to completion.

This is the only place in the framework that touches mutable state. It
orchestrates:

* Building the message list from instructions, optional session history,
  optional skill catalog, and the user input.
* Calling the provider in a loop, parsing tool calls, dispatching them, and
  feeding results back into the conversation.
* Handling structured output, multi-agent handoffs, human approval, and
  event hooks.

Public surface area is small: :meth:`Runner.run`, :meth:`Runner.run_streamed`,
and :meth:`Runner.run_stream`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Generic, TypeVar

from . import events
from .agent import Agent
from .checkpointer import Checkpointer, RunSnapshot
from .exceptions import MaxTurnsExceeded, OutputValidationError, UserError
from .guardrails import check_input_guardrails, check_output_guardrails
from .handoff import Handoff, _HandoffSignal, build_handoff_tool
from .hooks import dispatch
from .messages import AssistantMessage, ChatMessage, Usage, system, tool_message, user
from .output import (
    FINAL_OUTPUT_TOOL_NAME,
    OutputSpec,
    build_output_spec,
    loads_lenient,
    parse_output,
    response_format_for,
)
from .providers.base import Provider, StreamChunk
from .providers.openai_chat import OpenAIChatProvider
from .reliability import CancelToken, RetryPolicy, RunBudget
from .session import Session
from .tools import Tool, run_tool


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


@dataclass
class RunResult(Generic[TOutput]):
    """The terminal state of a completed run."""

    output: TOutput
    messages: list[ChatMessage]
    final_agent: Agent
    usage: Usage
    turns: int


class RunHandle(Generic[TOutput]):
    """Awaitable, async-iterable handle to a streamed run.

    Two equivalent ways to drive a run::

        # 1. Iterate the event stream and inspect the result at the end.
        handle = Runner.run_streamed(agent, "hi")
        async for event in handle:
            ...
        result = await handle.result()

        # 2. Just await it; events are consumed internally.
        result = await Runner.run_streamed(agent, "hi")

    Iteration is single-shot: a handle can be consumed exactly once. After
    iteration finishes (or an exception escapes), :meth:`result` returns the
    :class:`RunResult` (or re-raises the same exception).
    """

    def __init__(self, _stream: AsyncIterator[events.Event]) -> None:
        self._stream = _stream
        self._result: "RunResult[TOutput] | None" = None
        self._error: BaseException | None = None
        self._done = asyncio.Event()
        self._consumed = False

    def __aiter__(self) -> AsyncIterator[events.Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[events.Event]:
        if self._consumed:
            raise RuntimeError("RunHandle can only be iterated once")
        self._consumed = True
        try:
            async for ev in self._stream:
                if isinstance(ev, events.RunCompleted):
                    self._result = ev.result
                yield ev
        except BaseException as exc:
            self._error = exc
            self._done.set()
            raise
        else:
            self._done.set()

    async def result(self) -> "RunResult[TOutput]":
        """Return the final :class:`RunResult`, driving the stream if needed."""
        if not self._consumed:
            async for _ in self:
                pass
        else:
            await self._done.wait()
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise RuntimeError("Run completed without producing a result")
        return self._result

    def __await__(self):  # type: ignore[no-untyped-def]
        return self.result().__await__()


class Runner:
    """Stateless orchestrator. All entry points are class/static methods."""

    @staticmethod
    def run_streamed(
        agent: Agent[TContext, TOutput],
        input: "str | list[ChatMessage]",
        *,
        context: TContext | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        checkpointer: Checkpointer | None = None,
        run_id: str | None = None,
        resume_from: RunSnapshot | None = None,
        _parent_usage: Usage | None = None,
    ) -> "RunHandle[TOutput]":
        """Start a run and return a :class:`RunHandle`.

        The handle is both awaitable (for the final :class:`RunResult`) and
        async-iterable (for the event stream). See :class:`RunHandle`.
        """
        loop = _RunLoop(
            initial_agent=agent,
            user_input=input,
            context=context,
            session=session,
            session_id=session_id,
            max_turns=max_turns,
            parent_usage=_parent_usage,
            budget=budget,
            cancel_token=cancel_token,
            retry=retry,
            checkpointer=checkpointer,
            run_id=run_id,
            resume_from=resume_from,
        )
        return RunHandle(loop.stream())

    @staticmethod
    async def run(
        agent: Agent[TContext, TOutput],
        input: "str | list[ChatMessage]",
        *,
        context: TContext | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        checkpointer: Checkpointer | None = None,
        run_id: str | None = None,
        resume_from: RunSnapshot | None = None,
        _parent_usage: Usage | None = None,
    ) -> "RunResult[TOutput]":
        """Run ``agent`` to completion and return the final result."""
        return await Runner.run_streamed(
            agent,
            input,
            context=context,
            session=session,
            session_id=session_id,
            max_turns=max_turns,
            budget=budget,
            cancel_token=cancel_token,
            retry=retry,
            checkpointer=checkpointer,
            run_id=run_id,
            resume_from=resume_from,
            _parent_usage=_parent_usage,
        ).result()

    @staticmethod
    async def resume(
        agent: Agent[TContext, TOutput],
        *,
        checkpointer: Checkpointer,
        run_id: str,
        context: TContext | None = None,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
    ) -> "RunResult[TOutput]":
        """Resume a previously checkpointed run to completion.

        Loads the snapshot for ``run_id`` and continues the loop from the
        saved transcript. The opaque ``context`` value is *not* snapshotted —
        callers re-supply it here.
        """
        snapshot = await checkpointer.load(run_id)
        if snapshot is None:
            raise UserError(f"No snapshot found for run_id={run_id!r}")
        return await Runner.run(
            agent,
            input=[],  # ignored when resume_from is provided
            context=context,
            max_turns=max_turns,
            budget=budget,
            cancel_token=cancel_token,
            retry=retry,
            checkpointer=checkpointer,
            run_id=run_id,
            resume_from=snapshot,
        )

    @staticmethod
    async def run_stream(
        agent: Agent[TContext, TOutput],
        input: "str | list[ChatMessage]",
        *,
        context: TContext | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
    ) -> AsyncIterator[events.Event]:
        """Run ``agent`` and yield :class:`Event` instances as they happen."""
        async for event in Runner.run_streamed(
            agent,
            input,
            context=context,
            session=session,
            session_id=session_id,
            max_turns=max_turns,
            budget=budget,
            cancel_token=cancel_token,
            retry=retry,
        ):
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
        parent_usage: Usage | None = None,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        checkpointer: Checkpointer | None = None,
        run_id: str | None = None,
        resume_from: RunSnapshot | None = None,
    ) -> None:
        if session is not None and session_id is None:
            raise UserError("session_id is required when session is provided")
        self.agent = initial_agent
        self.user_input = user_input
        self.context = context
        self.session = session
        self.session_id = session_id
        self.max_turns = max_turns
        self.parent_usage = parent_usage
        self.budget = budget
        self.cancel_token = cancel_token
        self.retry = retry
        self.checkpointer = checkpointer
        self.run_id = run_id or (resume_from.run_id if resume_from else None)
        self.resume_from = resume_from

    async def stream(self) -> AsyncIterator[events.Event]:
        agent = self.agent

        # 1. Build the initial conversation: system prompt + (session history) + input.
        if self.resume_from is not None:
            # Resume: transcript = snapshot, skip session/input rebuild and
            # input guardrails (already vetted on the original run).
            transcript = list(self.resume_from.messages)
        else:
            transcript = await self._build_initial_messages(agent)
        run_ctx = RunContext(context=self.context, messages=transcript, agent=agent)
        if self.resume_from is not None:
            run_ctx.usage.add(self.resume_from.usage)

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
            # Input guardrails run once on the fully-built initial transcript.
            # Skip on resume — they already ran on the original input.
            if agent.input_guardrails and self.resume_from is None:
                await check_input_guardrails(
                    agent.input_guardrails, transcript, run_ctx
                )

            output: Any = None
            turns = self.resume_from.turns if self.resume_from is not None else 0
            output_repair_attempts = 0
            while True:
                if turns >= self.max_turns:
                    raise MaxTurnsExceeded(
                        f"Run exceeded max_turns={self.max_turns} without producing output"
                    )
                if self.cancel_token is not None:
                    self.cancel_token.check()
                if self.budget is not None:
                    self.budget.check(run_ctx.usage)
                turns += 1
                ev_turn = events.TurnStarted(agent=agent, turn=turns)
                yield ev_turn
                await dispatch(agent.hooks, ev_turn)

                providers = agent.resolve_providers()
                assistant = None
                async for chunk in _stream_with_fallback(
                    providers,
                    transcript,
                    tools=[t.openai_schema() for t in tools_by_name.values()] or None,
                    response_format=(
                        response_format_for(output_spec)
                        if output_spec and not output_spec.use_tool_fallback
                        else None
                    ),
                    settings=agent.settings,
                    retry=self.retry,
                ):
                    if chunk.text_delta is not None:
                        yield events.TextDelta(delta=chunk.text_delta)
                        await dispatch(
                            agent.hooks, events.TextDelta(delta=chunk.text_delta)
                        )
                    if chunk.reasoning_delta is not None:
                        ev_r = events.ReasoningDelta(delta=chunk.reasoning_delta)
                        yield ev_r
                        await dispatch(agent.hooks, ev_r)
                    if chunk.done is not None:
                        assistant = chunk.done

                if assistant is None:
                    # Provider exited without emitting ``done`` - shouldn't
                    # happen for well-behaved adapters, but be defensive.
                    raise RuntimeError("Provider stream ended without final message")

                run_ctx.usage.add(assistant.usage)
                if self.budget is not None:
                    self.budget.check(run_ctx.usage)
                msg = assistant.to_chat_message()
                transcript.append(msg)
                ev_msg = events.MessageCompleted(message=msg)
                yield ev_msg
                await dispatch(agent.hooks, ev_msg)

                # No tool calls -> we're done. Parse text or JSON output.
                if not assistant.tool_calls:
                    try:
                        output = await self._finalize_text_output(
                            assistant, output_spec
                        )
                    except OutputValidationError as exc:
                        if (
                            agent.output_repair
                            and output_spec is not None
                            and output_repair_attempts == 0
                        ):
                            output_repair_attempts += 1
                            transcript.append(user(_repair_prompt(exc)))
                            ev_end = events.TurnEnded(agent=agent, turn=turns)
                            yield ev_end
                            await dispatch(agent.hooks, ev_end)
                            continue
                        await dispatch(agent.hooks, events.ErrorOccurred(error=exc))
                        raise
                    ev_end = events.TurnEnded(agent=agent, turn=turns)
                    yield ev_end
                    await dispatch(agent.hooks, ev_end)
                    await self._snapshot(agent, transcript, run_ctx, turns)
                    break

                # Process tool calls. May trigger a handoff, in which case we
                # swap ``agent`` and continue the loop.
                handoff_signal: _HandoffSignal | None = None
                final_via_tool: Any = None
                for call in assistant.tool_calls:
                    if self.cancel_token is not None:
                        self.cancel_token.check()
                    if self.budget is not None:
                        self.budget.record_tool_call()
                        self.budget.check(run_ctx.usage)

                    if call.name == FINAL_OUTPUT_TOOL_NAME and output_spec is not None:
                        # Synthetic final-output tool: parse, ack, terminate.
                        final_via_tool = parse_output(output_spec, call.arguments)
                        transcript.append(tool_message(call.id, "ok"))
                        continue

                    tool = tools_by_name.get(call.name)
                    if tool is None:
                        err = f"Tool {call.name!r} is not available."
                        transcript.append(tool_message(call.id, err))
                        yield events.ToolCallCompleted(
                            call=call, result=err, is_error=True
                        )
                        await dispatch(
                            agent.hooks,
                            events.ToolCallCompleted(
                                call=call, result=err, is_error=True
                            ),
                        )
                        continue

                    try:
                        args = json.loads(call.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}

                    # Approval gate.
                    if tool.requires_approval(args, run_ctx):
                        loop = asyncio.get_running_loop()
                        fut: "asyncio.Future[bool]" = loop.create_future()
                        ev = events.ApprovalRequired(call=call, _future=fut)
                        yield ev
                        await dispatch(agent.hooks, ev)

                        # If a programmatic handler is configured and the
                        # streaming consumer hasn't already resolved the
                        # future, consult it. The handler may return:
                        #   * truthy / "allow" → approve
                        #   * "ask" → defer to streaming consumer
                        #   * falsy / "deny"  → reject
                        if agent.approval_handler is not None and not fut.done():
                            try:
                                decision = agent.approval_handler(call, run_ctx)
                                if inspect.isawaitable(decision):
                                    decision = await decision
                            except Exception as exc:
                                await dispatch(
                                    agent.hooks, events.ErrorOccurred(error=exc)
                                )
                                decision = False
                            if isinstance(decision, str):
                                token = decision.strip().lower()
                                if token == "ask":
                                    pass  # leave fut unresolved
                                elif token in ("allow", "approve", "yes"):
                                    fut.set_result(True)
                                else:
                                    fut.set_result(False)
                            elif not fut.done():
                                fut.set_result(bool(decision))

                        # No one resolved the future → default deny so the
                        # run cannot hang on an absent decision.
                        if not fut.done():
                            fut.set_result(False)

                        approved = fut.result()
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
                        result = await run_tool(tool, args, run_ctx)
                        is_error = False
                    except Exception as exc:
                        result = f"Tool error: {exc}"
                        is_error = True
                        await dispatch(agent.hooks, events.ErrorOccurred(error=exc))

                    if isinstance(result, _HandoffSignal):
                        # Defer the actual swap until after we've recorded the
                        # tool result, so the transcript stays consistent.
                        handoff_signal = result
                        result_text = f"Transferred to {result.target.name}" + (
                            f" ({result.reason})" if result.reason else ""
                        )
                    else:
                        result_text = _stringify_tool_result(result)

                    transcript.append(tool_message(call.id, result_text))
                    yield events.ToolCallCompleted(
                        call=call, result=result, is_error=is_error
                    )
                    await dispatch(
                        agent.hooks,
                        events.ToolCallCompleted(
                            call=call, result=result, is_error=is_error
                        ),
                    )

                ev_te = events.TurnEnded(agent=agent, turn=turns)
                yield ev_te
                await dispatch(agent.hooks, ev_te)
                await self._snapshot(agent, transcript, run_ctx, turns)

                if final_via_tool is not None:
                    output = final_via_tool
                    break

                if handoff_signal is not None:
                    prev_agent = agent
                    agent = handoff_signal.target
                    run_ctx.agent = agent
                    output_spec = build_output_spec(
                        agent.output_type, _supports_json_schema(agent)
                    )
                    tools_by_name = self._collect_tools(agent, mcp_tools, output_spec)
                    # Update system prompt for the new agent and optionally
                    # filter the inherited transcript.
                    transcript[:] = await self._reset_for_handoff(
                        transcript, agent, handoff_signal.handoff
                    )
                    handoff_ev = events.HandoffOccurred(
                        from_agent=prev_agent, to_agent=agent
                    )
                    yield handoff_ev
                    await dispatch(agent.hooks, handoff_ev)

            # Final bookkeeping.
            if output_spec is not None and output is None:
                # Should not happen, but be explicit.
                raise UserError(
                    f"Agent {agent.name!r} ended without producing structured output"
                )

            # Output guardrails: last gate before the result is returned.
            if agent.output_guardrails:
                await check_output_guardrails(agent.output_guardrails, output, run_ctx)

            result = RunResult(
                output=output,
                messages=transcript,
                final_agent=agent,
                usage=run_ctx.usage,
                turns=turns,
            )

            if self.session is not None:
                await self._persist_session(transcript)

            # Propagate usage into a parent run (e.g. agent_as_tool) so token
            # accounting accumulates across nested invocations.
            if self.parent_usage is not None:
                self.parent_usage.add(run_ctx.usage)

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

    async def _reset_for_handoff(
        self,
        transcript: list[ChatMessage],
        agent: Agent,
        handoff: Handoff | None,
    ) -> list[ChatMessage]:
        """Swap the leading system message when an agent handoff occurs.

        If the originating :class:`Handoff` declares an ``input_filter``, it is
        applied to the inherited transcript (excluding the old system prompt)
        before the new system prompt is prepended.
        """
        new_system = await self._system_prompt(agent)
        # Drop the leading system message if present; preserve the rest.
        body: list[ChatMessage] = list(transcript)
        if body and body[0].role == "system":
            body = body[1:]
        if handoff is not None and handoff.input_filter is not None:
            body = list(handoff.input_filter(body))
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

    async def _snapshot(
        self,
        agent: Agent,
        transcript: list[ChatMessage],
        run_ctx: RunContext,
        turns: int,
    ) -> None:
        """Persist a :class:`RunSnapshot` if a checkpointer is configured."""
        if self.checkpointer is None or self.run_id is None:
            return
        snapshot = RunSnapshot(
            run_id=self.run_id,
            agent_name=agent.name,
            messages=list(transcript),
            usage=Usage(
                input_tokens=run_ctx.usage.input_tokens,
                output_tokens=run_ctx.usage.output_tokens,
                cache_read_tokens=run_ctx.usage.cache_read_tokens,
                cache_write_tokens=run_ctx.usage.cache_write_tokens,
            ),
            turns=turns,
        )
        await self.checkpointer.save(snapshot)

    async def _finalize_text_output(
        self,
        assistant: AssistantMessage,
        output_spec: OutputSpec | None,
    ) -> Any:
        if output_spec is None:
            return assistant.content or ""
        # Either the model used the structured ``response_format`` path or it
        # was supposed to call the synthetic ``final_output`` tool. In both
        # cases, the remaining text content should parse as JSON describing the
        # target type. Any failure here surfaces as ``OutputValidationError``
        # and may be repaired in the main loop if the agent opts in.
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
    if isinstance(provider, OpenAIChatProvider):
        return provider.supports_json_schema
    return False


def _stringify_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, ensure_ascii=False)
    except TypeError:
        return str(result)


async def _unreachable_invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
    raise AssertionError("final_output tool must be intercepted by the runner")


def _repair_prompt(exc: OutputValidationError) -> str:
    """Build the repair message appended after a failed output validation."""
    return (
        "Your previous response could not be parsed into the expected output "
        f"type: {exc}. Please reply again with a response that exactly matches "
        "the required schema. Do not include any explanation, markdown, or "
        "code fences — only the JSON document."
    )


async def _stream_with_fallback(
    providers: list[Provider],
    messages: list[ChatMessage],
    *,
    tools: list[dict[str, Any]] | None,
    response_format: dict[str, Any] | None,
    settings: Any,
    retry: RetryPolicy | None,
) -> AsyncIterator[StreamChunk]:
    """Stream from the first provider that succeeds.

    For each provider we apply ``retry`` (if any) on errors that occur *before*
    any chunk has been forwarded. Once the stream starts producing data, the
    run is committed: a mid-stream error propagates immediately so we don't
    duplicate text or tool calls already seen by the caller.

    When all retries on a provider are exhausted, we move to the next provider
    in the chain. If all providers fail, the last exception is re-raised.
    """
    last_exc: BaseException | None = None
    max_attempts = retry.max_attempts if retry is not None else 1
    for provider in providers:
        attempt = 0
        while True:
            attempt += 1
            committed = False
            try:
                async for chunk in provider.stream(
                    messages,
                    tools=tools,
                    response_format=response_format,
                    settings=settings,
                ):
                    committed = True
                    yield chunk
                return  # success
            except BaseException as exc:
                last_exc = exc
                if committed:
                    raise
                if retry is not None and attempt < max_attempts and retry.retry_on(exc):
                    import random as _random

                    delay = min(
                        retry.backoff_max, retry.backoff_base * (2 ** (attempt - 1))
                    )
                    delay *= 0.5 + _random.random()
                    await retry.sleep(delay)
                    continue
                break  # next provider
    if last_exc is not None:
        raise last_exc


# Re-export for convenience.
__all__ = ["Runner", "RunContext", "RunResult", "RunHandle"]
