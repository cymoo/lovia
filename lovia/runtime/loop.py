"""Internal runtime that drives an :class:`Agent` to completion.

This is the only place in the framework that touches mutable state. It
orchestrates:

* Building the message list from instructions, optional session history,
  optional skill catalog, and the user input.
* Calling the provider in a loop, parsing tool calls, dispatching them, and
  feeding results back into the conversation.
* Handling structured output, multi-agent handoffs, human approval, and
  event hooks.

The public facade in :mod:`lovia.runner` owns the user-facing methods; this
module owns the mutable orchestration state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, cast

from .._types import JsonObject, JsonValue

if TYPE_CHECKING:
    from ..messages import AssistantTurn, Message

from .. import events
from .model_turn import stream_model_turn
from .state import TurnState
from .utils import (
    agent_model_label,
    input_preview,
    supports_json_schema,
    truncate_repr,
    unreachable_invoke,
)
from .tool_calls import ToolCallProcessor
from ..agent import Agent
from ..approvals import ApprovalChannel
from ..checkpointer import Checkpointer, RunSnapshot, RunStatus
from ..context import (
    CompactingContextPolicy,
    CompactionRequest,
    ContextPolicy,
    ContextResult,
)
from ..exceptions import (
    ContextOverflowError,
    MaxTurnsExceeded,
    OutputValidationError,
    UserError,
)
from ..guardrails import check_input_guardrails, check_output_guardrails
from ..handoff import Handoff, build_handoff_tool
from ..hooks import dispatch
from ..transcript import (
    TranscriptEntry,
    messages_to_entries,
    entries_to_messages,
    to_json_safe,
)
from ..transcript import InputEntry as _InputEntry
from ..transcript import ToolCallEntry as _ToolCallEntry
from ..transcript import ToolResultEntry as _ToolResultEntry
from ..messages import ToolCall, Usage, system
from ..output import (
    FINAL_OUTPUT_TOOL_NAME,
    DefaultOutputRepair,
    StructuredOutput,
    resolve_structured_output,
    loads_lenient,
    parse_structured_output,
)
from ..reliability import CancelToken, RetryPolicy, RunBudget
from ..run_context import RunContext
from .result import RunResult
from ..session import Session
from ..tools import Tool
from ..tracing import NoopTracer, Span, Tracer


logger = logging.getLogger(__name__)

Cleanup = Callable[[], Awaitable[None]]


@dataclass
class _BootstrapState:
    entries_log: list[TranscriptEntry]
    run_ctx: RunContext[object]
    mcp_tools: list[Tool]
    mcp_cleanup: list[Cleanup]
    structured_output: StructuredOutput | None
    tools_by_name: dict[str, Tool]
    turns: int


class RunLoop:
    """The actual event-producing async iterator.

    Kept as a class (rather than a long async generator) because it carries a
    small amount of mutable state across turns: the active agent, the
    transcript, accumulated usage, and the resolved structured-output policy.
    """

    def __init__(
        self,
        *,
        initial_agent: Agent,
        user_input: "str | list[Message]",
        context: object,
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
        append_instructions: "str | None" = None,
        output_type_override: object | None = None,
        delete_checkpoint_on_success: bool = False,
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
        self.context_policy: ContextPolicy = context_policy or CompactingContextPolicy()
        runtime = resume_from.runtime if resume_from is not None else {}
        # Tracks the input-token count from the previous turn so
        # ContextPolicy can prefer real usage over heuristic estimates.
        self._last_input_tokens = self._runtime_int(runtime, "last_input_tokens")
        # Per-run scratch the context policy may use to cache derived state
        # (e.g. a running summary). Owned here so it cannot leak across runs.
        self._compaction_scratch = self._runtime_dict(runtime, "compaction_scratch")
        self.run_id = run_id or (resume_from.run_id if resume_from else None)
        self.resume_from = resume_from
        self.delete_checkpoint_on_success = delete_checkpoint_on_success
        self.approvals = ApprovalChannel()
        # Per-call addendum appended to the initial agent's system prompt.
        # Applied once on the initial transcript; not re-applied across
        # handoffs (the new agent uses its own instructions verbatim).
        self.append_instructions = append_instructions
        # ``output_type=None`` means "use the active agent default", while
        # ``output_type=str`` means "force free-form text" for the whole run.
        self.output_type_override = output_type_override
        source = runtime.get("output_type_source")
        if self.output_type_override is not None:
            self._output_type_source = "run_override"
        else:
            self._output_type_source = (
                source if source in ("agent", "run_override") else "agent"
            )
        self._output_repair_attempts = self._runtime_int(
            runtime, "output_repair_attempts"
        ) or 0

    @staticmethod
    def _runtime_int(runtime: JsonObject, key: str) -> int | None:
        value = runtime.get(key)
        if isinstance(value, int):
            return value
        return None

    @staticmethod
    def _runtime_dict(runtime: JsonObject, key: str) -> dict[str, Any]:
        value = runtime.get(key)
        if isinstance(value, dict):
            return dict(value)
        return {}

    def _resolve_output_type(self, agent: Agent) -> object:
        """Return the output type to use for ``agent``.

        A Runner-level override is a run-wide final-output contract. When no
        override was supplied, each active agent uses its declared
        ``output_type``.
        """
        if self.output_type_override is not None:
            self._output_type_source = "run_override"
            return self.output_type_override
        self._output_type_source = "agent"
        return agent.output_type

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
        run_span: Span,
    ) -> AsyncIterator[events.Event]:
        bootstrap = await self._bootstrap_phase(agent)
        entries_log = bootstrap.entries_log
        run_ctx = bootstrap.run_ctx
        mcp_tools = bootstrap.mcp_tools
        mcp_cleanup = bootstrap.mcp_cleanup
        structured_output = bootstrap.structured_output
        tools_by_name = bootstrap.tools_by_name
        tool_processor = ToolCallProcessor(
            approvals=self.approvals,
            cancel_token=self.cancel_token,
            budget=self.budget,
        )

        ev_start = events.RunStarted(agent=agent)
        yield ev_start
        await dispatch(agent.hooks, ev_start)
        model_label = agent_model_label(agent)
        logger.info(
            "run.start: agent=%r model=%s input=%s",
            agent.name,
            model_label,
            truncate_repr(input_preview(self.user_input)),
        )
        output: object | None = None
        turns = bootstrap.turns
        terminal_checkpoint_saved = False
        try:
            # Input guardrails run once on the fully-built initial transcript.
            # Skip on resume — they already ran on the original input.
            if agent.input_guardrails and self.resume_from is None:
                await check_input_guardrails(
                    agent.input_guardrails,
                    entries_to_messages(entries_log),
                    run_ctx,
                )

            while True:
                pending_calls = self._pending_tool_calls(entries_log)
                if pending_calls:
                    state = TurnState()
                    turn_agent = agent
                    for call in pending_calls:
                        async for ev in tool_processor.process(
                            call,
                            agent=agent,
                            tools_by_name=tools_by_name,
                            run_ctx=run_ctx,
                            tracer=tracer,
                            structured_output=structured_output,
                            entries=entries_log,
                            state=state,
                        ):
                            yield ev
                            await dispatch(agent.hooks, ev)
                        await self._snapshot(agent, entries_log, run_ctx, turns)

                    ev_te = events.TurnEnded(agent=turn_agent, turn=turns)
                    yield ev_te
                    await dispatch(turn_agent.hooks, ev_te)

                    if state.final_via_tool is not None:
                        output = state.final_via_tool
                        break

                    if state.handoff_signal is not None:
                        logger.info(
                            "run.handoff: %r → %r",
                            agent.name,
                            state.handoff_signal.target.name,
                        )
                        (
                            agent,
                            structured_output,
                            tools_by_name,
                            handoff_ev,
                        ) = await self._handoff_phase(
                            agent,
                            state,
                            run_ctx,
                            tracer,
                            entries_log,
                            mcp_tools,
                            mcp_cleanup,
                        )
                        yield handoff_ev
                        await dispatch(agent.hooks, handoff_ev)

                    await self._snapshot(agent, entries_log, run_ctx, turns)
                    continue

                if turns >= self.max_turns:
                    logger.warning(
                        "run.max_turns: agent=%r turns=%d/%d",
                        agent.name,
                        turns,
                        self.max_turns,
                    )
                    raise MaxTurnsExceeded(
                        f"Run exceeded max_turns={self.max_turns} without producing output"
                    )
                if self.cancel_token is not None:
                    self.cancel_token.check()
                if self.budget is not None:
                    self.budget.check(run_ctx.usage)
                turns += 1
                logger.debug(
                    "run.turn.start: agent=%r turn=%d",
                    agent.name,
                    turns,
                )
                ev_turn = events.TurnStarted(agent=agent, turn=turns)
                yield ev_turn
                await dispatch(agent.hooks, ev_turn)

                providers = agent.resolve_providers()
                state = TurnState()

                # ContextPolicy: build the per-call VIEW of the transcript.
                # The view is sent to the provider for this turn only; the real
                # ``entries_log`` (and the Session) are never modified here.
                primary_provider = providers[0]
                policy_model = getattr(primary_provider, "model", None)
                req = CompactionRequest(
                    entries=entries_log,
                    provider=primary_provider,
                    model=policy_model,
                    last_input_tokens=self._last_input_tokens,
                    session_id=self.session_id,
                    run_id=self.run_id,
                    overflow=False,
                    scratch=self._compaction_scratch,
                )
                ctx_result = await self.context_policy.compact(req)
                view = await self._build_view(agent, entries_log, ctx_result)
                if ctx_result.changed:
                    async for ev in self._emit_compacted(
                        entries_log, view, ctx_result, reactive=False
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)
                # Reactive path: provider may report ContextOverflowError mid-stream.
                # Ask the policy for its more aggressive view, then retry the turn
                # exactly once. A second overflow propagates.
                try:
                    async for ev in stream_model_turn(
                        agent=agent,
                        providers=providers,
                        input_entries=view,
                        tools_by_name=tools_by_name,
                        structured_output=structured_output,
                        tracer=tracer,
                        turn=turns,
                        state=state,
                        retry=self.retry,
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)
                except ContextOverflowError as overflow:
                    logger.warning(
                        "context.overflow: provider raised; rebuilding a more "
                        "aggressive view (%s)",
                        overflow,
                    )
                    req.overflow = True
                    ctx_result = await self.context_policy.compact(req)
                    if not ctx_result.changed:
                        # Policy refused / couldn't shrink — surface original.
                        logger.error(
                            "context.overflow: policy could not shrink "
                            "transcript; surfacing ContextOverflowError"
                        )
                        raise
                    view = await self._build_view(agent, entries_log, ctx_result)
                    async for ev in self._emit_compacted(
                        entries_log, view, ctx_result, reactive=True
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)
                    state = TurnState()
                    async for ev in stream_model_turn(
                        agent=agent,
                        providers=providers,
                        input_entries=view,
                        tools_by_name=tools_by_name,
                        structured_output=structured_output,
                        tracer=tracer,
                        turn=turns,
                        state=state,
                        retry=self.retry,
                    ):
                        yield ev
                        await dispatch(agent.hooks, ev)
                assistant = state.assistant

                if assistant is None:
                    # Provider exited without emitting ``done`` - shouldn't
                    # happen for well-behaved adapters, but be defensive.
                    raise RuntimeError("Provider stream ended without final message")

                run_ctx.usage.add(assistant.usage)
                # Remember the real input-token count so the next turn's
                # ContextPolicy can size compaction against actual usage
                # rather than the chars/4 heuristic.
                if assistant.usage and assistant.usage.input_tokens:
                    self._last_input_tokens = assistant.usage.input_tokens
                if self.budget is not None:
                    self.budget.check(run_ctx.usage)
                turn_entries = state.turn_entries or []
                entries_log.extend(turn_entries)
                ev_msg = events.MessageCompleted(entries=turn_entries)
                yield ev_msg
                await dispatch(agent.hooks, ev_msg)

                # No tool calls -> we're done. Parse text or JSON output.
                if not assistant.tool_calls:
                    try:
                        output = await self._finalize_text_output(
                            assistant, structured_output
                        )
                    except OutputValidationError as exc:
                        repair_prompt = self._build_repair_prompt(
                            agent, exc, self._output_repair_attempts + 1
                        )
                        if repair_prompt is not None and structured_output is not None:
                            self._output_repair_attempts += 1
                            logger.warning(
                                "run.output_repair: agent=%r attempt=%d "
                                "schema=%s error=%s",
                                agent.name,
                                self._output_repair_attempts,
                                exc.output_type_name,
                                truncate_repr(str(exc)),
                            )
                            entries_log.append(
                                _InputEntry(role="user", content=repair_prompt)
                            )
                            ev_end = events.TurnEnded(agent=agent, turn=turns)
                            yield ev_end
                            await dispatch(agent.hooks, ev_end)
                            await self._snapshot(agent, entries_log, run_ctx, turns)
                            continue
                        await dispatch(agent.hooks, events.ErrorOccurred(error=exc))
                        raise
                    ev_end = events.TurnEnded(agent=agent, turn=turns)
                    yield ev_end
                    await dispatch(agent.hooks, ev_end)
                    break

                # Persist tool calls before executing them. Resume drains any
                # calls without matching ToolResultEntry by call_id.
                await self._snapshot(agent, entries_log, run_ctx, turns)
                continue

            result = await self._finalize_phase(
                agent,
                entries_log,
                run_ctx,
                turns,
                output,
                structured_output,
                run_span,
            )
            await self._complete_checkpoint(result)
            terminal_checkpoint_saved = True

            done = events.RunCompleted(result=result)
            yield done
            await dispatch(agent.hooks, done)
            logger.info(
                "run.done: agent=%r turns=%d tokens=%d(in=%d out=%d)",
                result.final_agent.name,
                result.turns,
                result.usage.total_tokens,
                result.usage.input_tokens,
                result.usage.output_tokens,
            )

        except Exception as exc:
            if not terminal_checkpoint_saved:
                await self._snapshot_failed_safely(
                    agent, entries_log, run_ctx, turns, exc
                )
            raise
        finally:
            for cleanup in mcp_cleanup:
                try:
                    await cleanup()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass

    # ------------------------------------------------------------------ helpers

    async def _bootstrap_phase(self, agent: Agent) -> _BootstrapState:
        """Initialize transcript, tools, structured output, and run context."""
        if self.resume_from is not None:
            entries_log: list[TranscriptEntry] = list(self.resume_from.entries)
        else:
            entries_log = await self._build_initial_entries(agent)
        run_ctx = RunContext(
            context=self.context,
            entries=entries_log,
            agent=agent,
            session_id=self.session_id,
        )
        if self.resume_from is not None:
            run_ctx.usage.add(self.resume_from.usage)

        mcp_cleanup: list[Cleanup] = []
        try:
            mcp_tools, mcp_cleanup = await self._connect_mcp(agent)
            sandbox_tools, sandbox_cleanup = await self._connect_sandbox(agent)
            mcp_cleanup.extend(sandbox_cleanup)
            structured_output = resolve_structured_output(
                self._resolve_output_type(agent), supports_json_schema(agent)
            )
            tools_by_name = self._collect_tools(
                agent, mcp_tools, sandbox_tools, structured_output
            )
        except Exception:
            for cleanup in mcp_cleanup:
                try:
                    await cleanup()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            raise
        turns = self.resume_from.turns if self.resume_from is not None else 0
        return _BootstrapState(
            entries_log=entries_log,
            run_ctx=run_ctx,
            mcp_tools=mcp_tools,
            mcp_cleanup=mcp_cleanup,
            structured_output=structured_output,
            tools_by_name=tools_by_name,
            turns=turns,
        )

    async def _handoff_phase(
        self,
        agent: Agent,
        state: TurnState,
        run_ctx: RunContext[object],
        tracer: Tracer,
        entries_log: list[TranscriptEntry],
        mcp_tools: list[Tool],
        cleanup: list[Cleanup],
    ) -> tuple[Agent, StructuredOutput | None, dict[str, Tool], events.HandoffOccurred]:
        """Switch active agent and rebuild agent-specific run state."""
        assert state.handoff_signal is not None
        prev_agent = agent
        with tracer.span(
            "handoff",
            from_agent=prev_agent.name,
            to_agent=state.handoff_signal.target.name,
        ):
            agent = state.handoff_signal.target
            run_ctx.agent = agent
            structured_output = resolve_structured_output(
                self._resolve_output_type(agent), supports_json_schema(agent)
            )
            sandbox_tools, sandbox_cleanup = await self._connect_sandbox(agent)
            cleanup.extend(sandbox_cleanup)
            tools_by_name = self._collect_tools(
                agent, mcp_tools, sandbox_tools, structured_output
            )
            entries_log[:] = await self._reset_for_handoff(
                entries_log, agent, state.handoff_signal.handoff
            )
        return (
            agent,
            structured_output,
            tools_by_name,
            events.HandoffOccurred(from_agent=prev_agent, to_agent=agent),
        )

    async def _finalize_phase(
        self,
        agent: Agent,
        entries_log: list[TranscriptEntry],
        run_ctx: RunContext[object],
        turns: int,
        output: object | None,
        structured_output: StructuredOutput | None,
        run_span: Span,
    ) -> RunResult:
        """Run final guardrails, persistence, usage propagation, and result build."""
        if structured_output is not None and output is None:
            raise UserError(
                f"Agent {agent.name!r} ended without producing structured output"
            )

        if agent.output_guardrails:
            await check_output_guardrails(agent.output_guardrails, output, run_ctx)

        result = RunResult(
            output=output,
            entries=entries_log,
            final_agent=agent,
            usage=run_ctx.usage,
            turns=turns,
        )

        if self.session is not None:
            await self._persist_session(entries_log)

        if self.parent_usage is not None:
            self.parent_usage.add(run_ctx.usage)

        run_span.set_attribute("turns", turns)
        run_span.set_attribute("total_tokens", run_ctx.usage.total_tokens)
        return result

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

    async def _build_initial_entries(self, agent: Agent) -> list[TranscriptEntry]:
        entries: list[TranscriptEntry] = []
        system_text = await self._system_prompt(agent, extra=self.append_instructions)
        if system_text:
            entries.append(_InputEntry(role="system", content=system_text))

        if self.session is not None:
            history_entries = await self.session.load(self.session_id)  # type: ignore[arg-type]
            entries.extend(history_entries)

        if isinstance(self.user_input, str):
            entries.append(_InputEntry(role="user", content=self.user_input))
        else:
            entries.extend(messages_to_entries(self.user_input))
        return entries

    async def _system_prompt(self, agent: Agent, *, extra: "str | None" = None) -> str:
        text = await agent.render_instructions(self.context, extra=extra)
        if agent.sandbox is not None:
            sandbox_instructions = agent.sandbox.instructions()
            if sandbox_instructions:
                text = f"{text}\n\n{sandbox_instructions}".strip()
        if agent.skills is not None:
            text = f"{text}\n\n{agent.skills.instructions()}".strip()
        return text

    async def _reset_for_handoff(
        self,
        entries: list[TranscriptEntry],
        agent: Agent,
        handoff: Handoff | None,
    ) -> list[TranscriptEntry]:
        """Swap the leading system message when an agent handoff occurs.

        If the originating :class:`Handoff` declares an ``input_filter``, it is
        applied to the inherited transcript (excluding the old system prompt)
        before the new system prompt is prepended.
        """
        new_system = await self._system_prompt(agent)
        body = entries_to_messages(entries)
        # Drop the leading system message if present; preserve the rest.
        if body and body[0].role == "system":
            body = body[1:]
        if handoff is not None and handoff.input_filter is not None:
            body = list(handoff.input_filter(body))
        if new_system:
            body = [system(new_system), *body]
        return messages_to_entries(body)

    def _collect_tools(
        self,
        agent: Agent,
        mcp_tools: list[Tool],
        sandbox_tools: list[Tool],
        structured_output: StructuredOutput | None,
    ) -> dict[str, Tool]:
        tools: dict[str, Tool] = {}

        def add_tool(source: str, t: Tool) -> None:
            if t.name in tools:
                hint = "Rename one tool or remove the duplicate."
                if source == "mcp":
                    hint = (
                        "Set MCPServer.name to prefix each server's tools "
                        "(e.g. name='fs' -> fs__read_file)."
                    )
                raise UserError(
                    f"Tool name conflict for {t.name!r} from {source}.",
                    hint=hint,
                )
            tools[t.name] = t

        for t in agent.tools:
            add_tool("agent.tools", t)
        for t in sandbox_tools:
            add_tool("agent.sandbox", t)
        for t in mcp_tools:
            add_tool("mcp", t)
        for h in agent.handoffs:
            handoff_obj = h if isinstance(h, Handoff) else Handoff(target=h)
            tool = build_handoff_tool(handoff_obj)
            add_tool("handoff", tool)
        if agent.skills is not None:
            for t in agent.skills.tools():
                add_tool("skills", t)
        if structured_output is not None and structured_output.use_tool_fallback:
            # Insert the synthetic ``final_output`` tool. Note we don't register
            # it as a real :class:`Tool` because the runner intercepts the call
            # by name; we only need its schema to be advertised to the model.
            add_tool(
                "output",
                Tool(
                    name=FINAL_OUTPUT_TOOL_NAME,
                    description="Call once with the final answer.",
                    parameters=structured_output.schema,
                    invoke=unreachable_invoke,
                ),
            )
        return tools

    async def _connect_mcp(self, agent: Agent) -> tuple[list[Tool], list[Cleanup]]:
        tools: list[Tool] = []
        cleanup: list[Cleanup] = []
        try:
            for server in agent.mcp_servers:
                conn = await server.open()
                if server.close_on_run:
                    cleanup.append(conn.close)
                tools.extend(conn.tools())
        except Exception:
            for close in reversed(cleanup):
                try:
                    await close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            raise
        return tools, cleanup

    async def _connect_sandbox(self, agent: Agent) -> tuple[list[Tool], list[Cleanup]]:
        if agent.sandbox is None:
            return [], []
        session = await agent.sandbox.open()
        cleanup: list[Cleanup] = []
        if agent.sandbox.close_on_run:
            cleanup.append(session.close)
        try:
            tools = agent.sandbox.tools(session)
        except Exception:
            for close in cleanup:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
            raise
        return tools, cleanup

    def _pending_tool_calls(self, entries: list[TranscriptEntry]) -> list[ToolCall]:
        completed_counts: dict[str, int] = {}
        for entry in entries:
            if isinstance(entry, _ToolResultEntry):
                completed_counts[entry.call_id] = (
                    completed_counts.get(entry.call_id, 0) + 1
                )

        seen_calls: dict[str, int] = {}
        pending: list[ToolCall] = []
        for entry in entries:
            if not isinstance(entry, _ToolCallEntry):
                continue
            index = seen_calls.get(entry.call_id, 0)
            seen_calls[entry.call_id] = index + 1
            if index >= completed_counts.get(entry.call_id, 0):
                pending.append(
                    ToolCall(
                        id=entry.call_id,
                        name=entry.name,
                        arguments=entry.arguments,
                    )
                )
        return pending

    def _snapshot_runtime(self) -> JsonObject:
        return cast(
            JsonObject,
            to_json_safe(
                {
                    "last_input_tokens": self._last_input_tokens,
                    "compaction_scratch": self._compaction_scratch,
                    "output_repair_attempts": self._output_repair_attempts,
                    "output_type_source": self._output_type_source,
                }
            )
            or {},
        )

    async def _complete_checkpoint(self, result: RunResult) -> None:
        if self.checkpointer is None or self.run_id is None:
            return
        if self.delete_checkpoint_on_success:
            await self.checkpointer.delete(self.run_id)
            return
        output = to_json_safe(result.output)
        error: JsonObject | None = None
        if output is None and result.output is not None:
            error = {
                "type": "OutputNotSerializable",
                "message": (
                    "Final output could not be serialized into JSON-safe "
                    "checkpoint payload."
                ),
            }
        await self._snapshot(
            result.final_agent,
            result.entries,
            RunContext(
                context=self.context,
                entries=result.entries,
                agent=result.final_agent,
                usage=result.usage,
                session_id=self.session_id,
            ),
            result.turns,
            status="completed",
            output=output,
            error=error,
        )

    async def _snapshot_failed_safely(
        self,
        agent: Agent,
        entries_log: list[TranscriptEntry],
        run_ctx: RunContext,
        turns: int,
        exc: Exception,
    ) -> None:
        try:
            await self._snapshot(
                agent,
                entries_log,
                run_ctx,
                turns,
                status="failed",
                error=self._error_payload(exc),
            )
        except Exception:  # noqa: BLE001 - don't mask the original run failure
            logger.exception("checkpoint.failed_snapshot: could not persist failure")

    def _error_payload(self, exc: Exception) -> JsonObject:
        return {
            "type": type(exc).__name__,
            "message": str(exc),
        }

    async def _snapshot(
        self,
        agent: Agent,
        entries_log: list[TranscriptEntry],
        run_ctx: RunContext,
        turns: int,
        *,
        status: RunStatus = "running",
        output: object | None = None,
        error: JsonObject | None = None,
    ) -> None:
        """Persist a :class:`RunSnapshot` if a checkpointer is configured."""
        if self.checkpointer is None or self.run_id is None:
            return
        snapshot = RunSnapshot(
            run_id=self.run_id,
            agent_name=agent.name,
            entries=list(entries_log),
            usage=Usage(
                input_tokens=run_ctx.usage.input_tokens,
                output_tokens=run_ctx.usage.output_tokens,
                cache_read_tokens=run_ctx.usage.cache_read_tokens,
                cache_write_tokens=run_ctx.usage.cache_write_tokens,
            ),
            turns=turns,
            status=status,
            output=to_json_safe(output),
            error=error,
            runtime=self._snapshot_runtime(),
        )
        await self.checkpointer.save(snapshot)

    async def _finalize_text_output(
        self,
        assistant: AssistantTurn,
        structured_output: StructuredOutput | None,
    ) -> object:
        if structured_output is None:
            return assistant.content or ""
        # Either the model used the structured ``response_format`` path or it
        # was supposed to call the synthetic ``final_output`` tool. In both
        # cases, the remaining text content should parse as JSON describing the
        # target type. Any failure here surfaces as ``OutputValidationError``
        # and may be repaired in the main loop if the agent opts in.
        try:
            return parse_structured_output(
                structured_output,
                cast(JsonValue, loads_lenient(assistant.content or "")),
            )
        except OutputValidationError as exc:
            if exc.output_type_name is None:
                exc.output_type_name = getattr(
                    structured_output.output_type,
                    "__name__",
                    str(structured_output.output_type),
                )
            raise

    async def _persist_session(self, entries_log: list[TranscriptEntry]) -> None:
        # Replace stored transcript with the latest (simple and predictable).
        # System prompts are agent-owned and re-rendered each run, so any
        # system :class:`InputEntry` is excluded from the persisted
        # history.
        assert self.session is not None and self.session_id is not None
        body = [
            it
            for it in entries_log
            if not (isinstance(it, _InputEntry) and it.role == "system")
        ]
        await self.session.replace(self.session_id, body)

    async def _build_view(
        self,
        agent: Agent,
        entries_log: list[TranscriptEntry],
        result: ContextResult,
    ) -> list[TranscriptEntry]:
        """Return the per-call view to send to the provider.

        Compaction is view-only: ``entries_log`` is never mutated. When the
        policy dropped the leading system message (e.g. it summarized the head),
        re-prepend it so provider adapters still see one.
        """
        if not result.changed:
            return entries_log
        view = result.entries
        if view and isinstance(view[0], _InputEntry) and view[0].role == "system":
            return view
        system_text = await self._system_prompt(agent)
        if system_text:
            return [_InputEntry(role="system", content=system_text), *view]
        return view

    async def _emit_compacted(
        self,
        entries_before: list[TranscriptEntry],
        view: list[TranscriptEntry],
        result: ContextResult,
        *,
        reactive: bool,
    ) -> AsyncIterator[events.Event]:
        """Emit ``ContextCompacted``. Does not mutate state or persist anything."""
        ev = events.ContextCompacted(
            session_id=self.session_id,
            entries_before=list(entries_before),
            entries_after=list(view),
            summary=result.summary,
            reactive=reactive,
            reason=result.reason or "context_policy",
            metadata=result.metadata,
        )
        yield ev


__all__ = ["RunLoop"]
