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
from dataclasses import dataclass
from typing import Any, AsyncIterator, TypeVar

from . import events
from .agent import Agent
from .checkpointer import Checkpointer, RunSnapshot
from .context_policy import (
    ContextPolicy,
    NoopContextPolicy,
    PolicyContext,
)
from .exceptions import (
    ContextOverflowError,
    MaxTurnsExceeded,
    OutputValidationError,
    UserError,
)
from .guardrails import check_input_guardrails, check_output_guardrails
from .handoff import Handoff, _HandoffSignal, build_handoff_tool
from .hooks import dispatch
from .items import (
    FinishDelta,
    Item,
    ItemDelta,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
    assistant_to_items,
    items_to_chat_messages,
    transcript_to_items,
)
from .items import InputMessageItem as _InputMessageItem
from .items import ToolCallOutputItem as _ToolCallOutputItem
from .messages import AssistantMessage, ChatMessage, ToolCall, Usage, system, tool_message, user
from .output import (
    FINAL_OUTPUT_TOOL_NAME,
    DefaultOutputRepair,
    OutputSpec,
    build_output_spec,
    loads_lenient,
    parse_output,
    response_format_for,
)
from .providers.base import Provider
from .providers.openai_chat import OpenAIChatProvider
from .reliability import CancelToken, RetryPolicy, RunBudget
from .run_context import RunContext
from .approvals import ApprovalChannel
from .session import Session
from .tools import Tool, render_tool_result, run_tool
from .tracing import NoopTracer, Tracer


TContext = TypeVar("TContext")


@dataclass
class _TurnState:
    """Mutable scratch space populated by phase helpers within one turn.

    Async generators can't easily return a value, so the orchestrator passes
    in a fresh ``_TurnState`` and reads the populated fields once the helper
    finishes yielding events.
    """

    assistant: AssistantMessage | None = None
    handoff_signal: "_HandoffSignal | None" = None
    final_via_tool: Any = None


@dataclass
class RunResult:
    """The terminal state of a completed run.

    ``output`` is typed ``Any`` because the structured output type is a
    runtime field on :class:`Agent`, not a static generic parameter — call
    sites that know their output type can cast or annotate locally.

    The transcript lives in ``new_items`` (the canonical Item-list form
    consumed by other agents, Session storage, and the Responses API).
    ``messages`` is a derived view in OpenAI Chat wire format, convenient
    for printing or forwarding to legacy clients.
    """

    output: Any
    new_items: list[Item]
    final_agent: Agent
    usage: Usage
    turns: int

    @property
    def messages(self) -> list[ChatMessage]:
        """ChatMessage view derived from :attr:`new_items`.

        Useful for inspecting the transcript in OpenAI-Chat wire format.
        """
        from .items import items_to_chat_messages

        return items_to_chat_messages(self.new_items)


class RunHandle:
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

    def __init__(self, _stream: AsyncIterator[events.Event], approvals: "ApprovalChannel") -> None:
        self._stream = _stream
        self._result: "RunResult | None" = None
        self._error: BaseException | None = None
        self._done = asyncio.Event()
        self._consumed = False
        #: Out-of-band channel for resolving :class:`events.ApprovalRequired`
        #: by ``ToolCall.id``. Streaming consumers normally use the helpers
        #: on the event directly; the channel is for resolving from a
        #: different task (e.g. a UI-thread callback).
        self.approvals = approvals

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

    async def result(self) -> "RunResult":
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
        agent: Agent[TContext],
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
        context_policy: ContextPolicy | None = None,
        run_id: str | None = None,
        resume_from: RunSnapshot | None = None,
        _parent_usage: Usage | None = None,
    ) -> "RunHandle":
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
            context_policy=context_policy,
            run_id=run_id,
            resume_from=resume_from,
        )
        return RunHandle(loop.stream(), loop.approvals)

    @staticmethod
    async def run(
        agent: Agent[TContext],
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
        context_policy: ContextPolicy | None = None,
        run_id: str | None = None,
        resume_from: RunSnapshot | None = None,
        _parent_usage: Usage | None = None,
    ) -> "RunResult":
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
            context_policy=context_policy,
            run_id=run_id,
            resume_from=resume_from,
            _parent_usage=_parent_usage,
        ).result()

    @staticmethod
    async def resume(
        agent: Agent[TContext],
        *,
        checkpointer: Checkpointer,
        run_id: str,
        context: TContext | None = None,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
    ) -> "RunResult":
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
        agent: Agent[TContext],
        input: "str | list[ChatMessage]",
        *,
        context: TContext | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        context_policy: ContextPolicy | None = None,
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
            context_policy=context_policy,
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
        context_policy: ContextPolicy | None = None,
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
        self.context_policy: ContextPolicy = context_policy or NoopContextPolicy()
        # Tracks the prompt-token count from the previous turn so
        # ContextPolicy can prefer real usage over heuristic estimates.
        self._last_prompt_tokens: int | None = None
        self.run_id = run_id or (resume_from.run_id if resume_from else None)
        self.resume_from = resume_from
        self.approvals = ApprovalChannel()

    async def stream(self) -> AsyncIterator[events.Event]:
        agent = self.agent
        tracer: Tracer = agent.tracer or NoopTracer()

        with tracer.span(
            "run",
            agent=agent.name,
            run_id=self.run_id,
            resumed=self.resume_from is not None,
        ) as run_span:
            async for ev in self._stream_inner(agent, tracer, run_span):
                yield ev

    async def _stream_inner(
        self,
        agent: Agent,
        tracer: Tracer,
        run_span: Any,
    ) -> AsyncIterator[events.Event]:
        # 1. Build the initial conversation: system prompt + (session history) + input.
        if self.resume_from is not None:
            # Resume: rebuild transcript from the snapshot's items.
            items_log: list[Item] = list(self.resume_from.items)
            transcript = items_to_chat_messages(items_log)
        else:
            transcript = await self._build_initial_messages(agent)
            # Mirror the transcript as Items. Stays in lockstep with
            # ``transcript`` for the whole run — invariant verified by tests.
            items_log = transcript_to_items(transcript)
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
                state = _TurnState()

                # ContextPolicy: rewrite the transcript before the model
                # call. Identity check skips the no-op path.
                primary_provider = providers[0] if providers else None
                policy_model = getattr(primary_provider, "model", None)
                policy_ctx = PolicyContext(
                    provider=primary_provider,
                    model=policy_model,
                    last_prompt_tokens=self._last_prompt_tokens,
                    session_id=self.session_id,
                )
                items_before = items_log
                new_items = await self.context_policy.apply(items_before, ctx=policy_ctx)
                if new_items is not items_before:
                    async for ev in self._on_context_compacted(
                        agent, items_before, new_items, reactive=False,
                        transcript=transcript, items_log=items_log,
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)
                    # _on_context_compacted mutates items_log/transcript in place
                # Reactive path: provider may report ContextOverflowError mid-stream.
                # Catch it, ask the policy for its more aggressive compaction,
                # then retry the turn exactly once. A second overflow propagates.
                try:
                    async for ev in self._run_model_turn(
                        agent, providers, items_log, tools_by_name, output_spec, tracer, turns, state
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)
                except ContextOverflowError:
                    items_before = items_log
                    new_items = await self.context_policy.apply_reactive(
                        items_before, ctx=policy_ctx
                    )
                    if new_items is items_before:
                        # Policy refused / couldn't shrink — surface original.
                        raise
                    async for ev in self._on_context_compacted(
                        agent, items_before, new_items, reactive=True,
                        transcript=transcript, items_log=items_log,
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)
                    state = _TurnState()
                    async for ev in self._run_model_turn(
                        agent, providers, items_log, tools_by_name, output_spec, tracer, turns, state
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)
                assistant = state.assistant

                if assistant is None:
                    # Provider exited without emitting ``done`` - shouldn't
                    # happen for well-behaved adapters, but be defensive.
                    raise RuntimeError("Provider stream ended without final message")

                run_ctx.usage.add(assistant.usage)
                # Remember the real prompt-token count so the next turn's
                # ContextPolicy can size compaction against actual usage
                # rather than the chars/4 heuristic.
                if assistant.usage and assistant.usage.input_tokens:
                    self._last_prompt_tokens = assistant.usage.input_tokens
                if self.budget is not None:
                    self.budget.check(run_ctx.usage)
                msg = assistant.to_chat_message()
                transcript.append(msg)
                turn_items = assistant_to_items(assistant)
                items_log.extend(turn_items)
                ev_msg = events.MessageCompleted(items=turn_items)
                yield ev_msg
                await dispatch(agent.hooks, ev_msg)

                # No tool calls -> we're done. Parse text or JSON output.
                if not assistant.tool_calls:
                    try:
                        output = await self._finalize_text_output(
                            assistant, output_spec
                        )
                    except OutputValidationError as exc:
                        repair_prompt = self._build_repair_prompt(
                            agent, exc, output_repair_attempts + 1
                        )
                        if repair_prompt is not None and output_spec is not None:
                            output_repair_attempts += 1
                            transcript.append(user(repair_prompt))
                            items_log.append(
                                _InputMessageItem(role="user", content=repair_prompt)
                            )
                            ev_end = events.TurnEnded(agent=agent, turn=turns)
                            yield ev_end
                            await dispatch(agent.hooks, ev_end)
                            continue
                        await dispatch(agent.hooks, events.ErrorOccurred(error=exc))
                        raise
                    ev_end = events.TurnEnded(agent=agent, turn=turns)
                    yield ev_end
                    await dispatch(agent.hooks, ev_end)
                    await self._snapshot(agent, items_log, run_ctx, turns)
                    break

                # Process tool calls. May trigger a handoff, in which case we
                # swap ``agent`` and continue the loop. State is collected on
                # ``state`` so the helper can report outcomes without leaking
                # control flow back to the orchestrator.
                for call in assistant.tool_calls:
                    async for ev in self._process_tool_call(
                        call, agent, tools_by_name, run_ctx, tracer, output_spec,
                        transcript, items_log, state,
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)

                ev_te = events.TurnEnded(agent=agent, turn=turns)
                yield ev_te
                await dispatch(agent.hooks, ev_te)
                await self._snapshot(agent, items_log, run_ctx, turns)

                if state.final_via_tool is not None:
                    output = state.final_via_tool
                    break

                if state.handoff_signal is not None:
                    prev_agent = agent
                    with tracer.span(
                        "handoff",
                        from_agent=prev_agent.name,
                        to_agent=state.handoff_signal.target.name,
                    ):
                        agent = state.handoff_signal.target
                        run_ctx.agent = agent
                        output_spec = build_output_spec(
                            agent.output_type, _supports_json_schema(agent)
                        )
                        tools_by_name = self._collect_tools(agent, mcp_tools, output_spec)
                        # Update system prompt for the new agent and optionally
                        # filter the inherited transcript.
                        transcript[:] = await self._reset_for_handoff(
                            transcript, agent, state.handoff_signal.handoff
                        )
                        # Mirror the reset on items_log so the invariant
                        # ``items_to_chat_messages(items_log) ≈ transcript``
                        # keeps holding after handoffs.
                        items_log[:] = transcript_to_items(transcript)
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
                new_items=items_log,
                final_agent=agent,
                usage=run_ctx.usage,
                turns=turns,
            )

            if self.session is not None:
                await self._persist_session(items_log)

            # Propagate usage into a parent run (e.g. agent_as_tool) so token
            # accounting accumulates across nested invocations.
            if self.parent_usage is not None:
                self.parent_usage.add(run_ctx.usage)

            done = events.RunCompleted(result=result)
            yield done
            await dispatch(agent.hooks, done)
            run_span.set_attribute("turns", turns)
            run_span.set_attribute("total_tokens", run_ctx.usage.total_tokens)

        finally:
            for cleanup in mcp_cleanup:
                try:
                    await cleanup()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

    # ------------------------------------------------------------------ helpers

    def _build_repair_prompt(
        self, agent: Agent, exc: OutputValidationError, attempt: int
    ) -> str | None:
        """Resolve the agent's :attr:`output_repair` policy for one failure.

        Returns the user-prompt to append, or ``None`` to stop retrying.
        Supports both the ergonomic ``bool`` form and a full
        :class:`~lovia.output.OutputRepairStrategy` instance.
        """
        policy = agent.output_repair
        if policy is False:
            return None
        if policy is True:
            policy = DefaultOutputRepair()
        return policy.build_prompt(exc, attempt)

    async def _run_model_turn(
        self,
        agent: Agent,
        providers: list[Provider],
        input_items: list[Item],
        tools_by_name: dict[str, Tool],
        output_spec: OutputSpec | None,
        tracer: Tracer,
        turn: int,
        state: _TurnState,
    ) -> AsyncIterator[events.Event]:
        """Stream one model call: yield deltas and capture the final message.

        Yields :class:`events.TextDelta` and :class:`events.ReasoningDelta`
        as the provider streams. Assembles incoming :class:`ItemDelta` values
        into the final :class:`AssistantMessage` stored on ``state.assistant``.
        """
        model_label = getattr(providers[0], "model", None) if providers else None

        # Incremental state assembled from the provider's delta stream.
        text_buf: list[str] = []
        reasoning_buf: list[str] = []
        # index -> {id, name, arguments}. We use a dict (not list) because
        # providers can emit deltas for indices out of order.
        tc_slots: dict[int, dict[str, str]] = {}
        usage = Usage()
        finish_reason: str | None = None

        with tracer.span("model_call", model=model_label, turn=turn):
            async for delta in _stream_with_fallback(
                providers,
                input_items,
                tools=[t.openai_schema() for t in tools_by_name.values()] or None,
                response_format=(
                    response_format_for(output_spec)
                    if output_spec and not output_spec.use_tool_fallback
                    else None
                ),
                settings=agent.settings,
                retry=self.retry,
            ):
                if isinstance(delta, TextDelta):
                    text_buf.append(delta.text)
                    yield events.TextDelta(delta=delta.text)
                elif isinstance(delta, ReasoningDelta):
                    reasoning_buf.append(delta.text)
                    yield events.ReasoningDelta(delta=delta.text)
                elif isinstance(delta, ToolCallDelta):
                    slot = tc_slots.setdefault(
                        delta.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if delta.call_id:
                        slot["id"] = delta.call_id
                    if delta.name:
                        slot["name"] = delta.name
                    if delta.arguments:
                        slot["arguments"] += delta.arguments
                elif isinstance(delta, UsageDelta):
                    usage = delta.usage
                elif isinstance(delta, FinishDelta):
                    finish_reason = delta.reason

        state.assistant = AssistantMessage(
            content="".join(text_buf) or None,
            reasoning_content="".join(reasoning_buf) or None,
            tool_calls=[
                ToolCall(id=s["id"], name=s["name"], arguments=s["arguments"] or "{}")
                for _, s in sorted(tc_slots.items())
            ],
            usage=usage,
            finish_reason=finish_reason,
        )

    async def _process_tool_call(
        self,
        call: Any,
        agent: Agent,
        tools_by_name: dict[str, Tool],
        run_ctx: RunContext[Any],
        tracer: Tracer,
        output_spec: OutputSpec | None,
        transcript: list[ChatMessage],
        items_log: list[Item],
        state: _TurnState,
    ) -> AsyncIterator[events.Event]:
        """Handle one tool call from the assistant.

        Yields approval / start / completion events as appropriate, appends
        the tool result to the transcript (and its Item mirror), and records
        handoff / final-output outcomes on ``state``. The caller is
        responsible for forwarding yielded events to hooks and to the outer
        stream.
        """
        # Pre-flight: cancellation and budget.
        if self.cancel_token is not None:
            self.cancel_token.check()
        if self.budget is not None:
            self.budget.record_tool_call()
            self.budget.check(run_ctx.usage)

        # Synthetic final-output tool: parse, ack, terminate.
        if call.name == FINAL_OUTPUT_TOOL_NAME and output_spec is not None:
            state.final_via_tool = parse_output(output_spec, call.arguments)
            transcript.append(tool_message(call.id, "ok"))
            items_log.append(_ToolCallOutputItem(call_id=call.id, output="ok"))
            return

        tool = tools_by_name.get(call.name)
        if tool is None:
            err = f"Tool {call.name!r} is not available."
            transcript.append(tool_message(call.id, err))
            items_log.append(
                _ToolCallOutputItem(call_id=call.id, output=err, is_error=True)
            )
            yield events.ToolCallCompleted(call=call, result=err, is_error=True)
            return

        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        # Approval gate (only if the tool requested it).
        if tool.requires_approval(args, run_ctx):
            async for ev in self._await_approval(call, agent, run_ctx):
                yield ev
            approved = self.approvals.register(call.id).result()
            if not approved:
                denial = f"Tool {call.name} was not approved."
                transcript.append(tool_message(call.id, denial))
                items_log.append(
                    _ToolCallOutputItem(call_id=call.id, output=denial, is_error=True)
                )
                yield events.ToolCallCompleted(call=call, result=denial, is_error=True)
                return

        yield events.ToolCallStarted(call=call)

        try:
            with tracer.span("tool", name=tool.name, call_id=call.id):
                result = await run_tool(
                    tool,
                    args,
                    run_ctx,
                    default_retries=agent.default_tool_retries,
                    default_timeout=agent.default_tool_timeout,
                )
            is_error = False
        except Exception as exc:
            result = f"Tool error: {exc}"
            is_error = True
            yield events.ErrorOccurred(error=exc)

        if isinstance(result, _HandoffSignal):
            # Defer the actual swap until after we've recorded the tool
            # result, so the transcript stays consistent.
            state.handoff_signal = result
            result_text = f"Transferred to {result.target.name}" + (
                f" ({result.reason})" if result.reason else ""
            )
        else:
            result_text = await render_tool_result(
                tool, result, run_ctx, default=agent.tool_result_renderer
            )

        transcript.append(tool_message(call.id, result_text))
        # ``raw`` preserves the Python-side return value (or the handoff
        # signal) so downstream Item consumers can introspect it without
        # re-parsing the rendered string.
        items_log.append(
            _ToolCallOutputItem(
                call_id=call.id,
                output=result_text,
                raw=result,
                is_error=is_error,
            )
        )
        yield events.ToolCallCompleted(call=call, result=result, is_error=is_error)

    async def _await_approval(
        self,
        call: Any,
        agent: Agent,
        run_ctx: RunContext[Any],
    ) -> AsyncIterator[events.Event]:
        """Yield :class:`events.ApprovalRequired` and resolve the channel.

        Three resolution paths race here:

        1. A streaming consumer calls ``ev.approve()`` / ``ev.reject()``.
        2. The agent's ``approval_handler`` returns a verdict.
        3. An out-of-band caller resolves via ``RunHandle.approvals``.

        If none of the above resolve the request, the runner falls back to
        **deny** so the run cannot hang on an absent decision.
        """
        fut = self.approvals.register(call.id)
        yield events.ApprovalRequired(call=call, _channel=self.approvals)

        # If a programmatic handler is configured and no one has resolved
        # the future yet, consult it. The handler may return:
        #   * truthy / "allow" → approve
        #   * "ask" → defer to streaming consumer / out-of-band caller
        #   * falsy / "deny" → reject
        if agent.approval_handler is not None and not fut.done():
            try:
                decision = agent.approval_handler(call, run_ctx)
                if inspect.isawaitable(decision):
                    decision = await decision
            except Exception as exc:
                yield events.ErrorOccurred(error=exc)
                decision = False
            if isinstance(decision, str):
                token = decision.strip().lower()
                if token == "ask":
                    pass  # leave fut unresolved
                elif token in ("allow", "approve", "yes"):
                    self.approvals.approve(call.id)
                else:
                    self.approvals.reject(call.id)
            elif not fut.done():
                self.approvals._resolve(call.id, bool(decision))

        # Default-deny if still unresolved.
        if not fut.done():
            self.approvals.reject(call.id)

    async def _build_initial_messages(self, agent: Agent) -> list[ChatMessage]:
        msgs: list[ChatMessage] = []
        system_text = await self._system_prompt(agent)
        if system_text:
            msgs.append(system(system_text))

        if self.session is not None:
            # Session stores Items (the canonical form); flatten to wire
            # format for the in-flight transcript.
            history_items = await self.session.load(self.session_id)  # type: ignore[arg-type]
            msgs.extend(items_to_chat_messages(history_items))

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
        items_log: list[Item],
        run_ctx: RunContext,
        turns: int,
    ) -> None:
        """Persist a :class:`RunSnapshot` if a checkpointer is configured."""
        if self.checkpointer is None or self.run_id is None:
            return
        snapshot = RunSnapshot(
            run_id=self.run_id,
            agent_name=agent.name,
            items=list(items_log),
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

    async def _persist_session(self, items_log: list[Item]) -> None:
        # Replace stored transcript with the latest (simple and predictable).
        # System prompts are agent-owned and re-rendered each run, so any
        # system :class:`InputMessageItem` is excluded from the persisted
        # history.
        assert self.session is not None and self.session_id is not None
        body = [
            it
            for it in items_log
            if not (isinstance(it, _InputMessageItem) and it.role == "system")
        ]
        # Prefer the atomic ``replace`` when available (added with
        # ContextPolicy in lovia 0.x); fall back to clear+append otherwise
        # so external session implementations keep working unchanged.
        replace = getattr(self.session, "replace", None)
        if callable(replace):
            await replace(self.session_id, body)
        else:
            await self.session.clear(self.session_id)
            await self.session.append(self.session_id, body)

    async def _on_context_compacted(
        self,
        agent: Agent,
        items_before: list[Item],
        items_after: list[Item],
        *,
        reactive: bool,
        transcript: list[ChatMessage],
        items_log: list[Item],
    ) -> AsyncIterator[events.Event]:
        """Persist a compacted transcript and emit ``ContextCompacted``.

        Mutates ``items_log`` and ``transcript`` in place so the existing
        loop continues to operate on the rewritten conversation. Tries to
        recover a summary string by inspecting the new head item — the
        :class:`SummarizingContextPolicy` always emits a single
        :class:`InputMessageItem` with the agreed prefix, so we strip the
        marker rather than asking the policy to return a richer object.
        """
        snapshot_before = list(items_before)
        items_log[:] = items_after
        # Keep the wire-format mirror in lockstep.
        transcript[:] = items_to_chat_messages(items_log)
        # Re-prepend the system prompt that ``_build_initial_messages`` put
        # at the start of the original transcript — it's not stored in
        # ``items_log`` for some shapes, and the agent expects it back.
        await self._reinstate_system_prompt(agent, transcript)
        # Persist the rewritten transcript so a crash + restart picks up
        # the compacted version (the whole point of permanent compaction).
        if self.session is not None and self.session_id is not None:
            await self._persist_session(items_log)
        summary = _extract_summary(items_after)
        ev = events.ContextCompacted(
            session_id=self.session_id,
            items_before=snapshot_before,
            items_after=list(items_after),
            summary=summary,
            reactive=reactive,
        )
        yield ev

    async def _reinstate_system_prompt(
        self, agent: Agent, transcript: list[ChatMessage]
    ) -> None:
        """Ensure the agent's system prompt is the first message after a rewrite.

        Compaction operates on ``items_log`` which may or may not start with
        a system message; provider adapters expect one, so we re-render and
        prepend if missing.
        """
        if transcript and transcript[0].role == "system":
            return
        system_text = await self._system_prompt(agent)
        if system_text:
            transcript.insert(0, system(system_text))

    async def _emit(self, event: events.Event, agent: Agent) -> None:
        # Hooks-only dispatch. Used for events the loop itself yields elsewhere.
        await dispatch(agent.hooks, event)


def _supports_json_schema(agent: Agent) -> bool:
    """Whether the agent's provider can use OpenAI-style ``response_format``."""
    provider = agent.resolve_provider() if isinstance(agent.model, str) else agent.model
    if isinstance(provider, OpenAIChatProvider):
        return provider.supports_json_schema
    return False


# The marker SummarizingContextPolicy wraps its summary text in, mirrored here
# so the runner can surface the raw summary on ``ContextCompacted`` events
# without coupling to context_policy internals at import time.
_SUMMARY_OPEN = "[Conversation summary — prior turns compacted]"
_SUMMARY_CLOSE = "[End summary]"


def _extract_summary(items: list[Item]) -> str | None:
    """Best-effort extraction of summary text from a compacted item list.

    Returns ``None`` for structural-only compaction (no LLM summary
    produced) so the event payload reflects what actually happened.
    """
    if not items:
        return None
    head = items[0]
    if not isinstance(head, _InputMessageItem):
        return None
    content = head.content
    if not isinstance(content, str):
        return None
    if _SUMMARY_OPEN not in content:
        return None
    body = content.split(_SUMMARY_OPEN, 1)[1]
    if _SUMMARY_CLOSE in body:
        body = body.rsplit(_SUMMARY_CLOSE, 1)[0]
    return body.strip()


async def _unreachable_invoke(args: dict[str, Any], ctx: "RunContext") -> Any:
    raise AssertionError("final_output tool must be intercepted by the runner")


async def _stream_with_fallback(
    providers: list[Provider],
    input_items: list[Item],
    *,
    tools: list[dict[str, Any]] | None,
    response_format: dict[str, Any] | None,
    settings: Any,
    retry: RetryPolicy | None,
) -> AsyncIterator[ItemDelta]:
    """Stream from the first provider that succeeds.

    For each provider we apply ``retry`` (if any) on errors that occur *before*
    any delta has been forwarded. Once the stream starts producing data, the
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
                async for delta in provider.stream(
                    input_items,
                    tools=tools,
                    response_format=response_format,
                    settings=settings,
                ):
                    committed = True
                    yield delta
                return  # success
            except BaseException as exc:
                last_exc = exc
                if committed:
                    raise
                # ContextOverflowError must surface to the runner's reactive
                # compaction loop; falling back to another provider is
                # pointless because the prompt size hasn't changed.
                from .exceptions import ContextOverflowError as _COE

                if isinstance(exc, _COE):
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
