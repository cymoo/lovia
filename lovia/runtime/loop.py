"""Internal runtime that drives an :class:`Agent` to completion.

This is the only place in the framework that touches mutable run state. It
orchestrates:

* Building the transcript from instructions, optional session history, and
  the user input.
* The turn loop: one iteration is one full turn — a model call followed by
  the execution of any tool calls it requested.
* Structured output, multi-agent handoffs, human approval, context
  compaction, checkpointing, and event hooks.

The public facade in :mod:`lovia.runner` owns the user-facing methods; this
module owns the orchestration. All mutable run state lives in
:class:`~lovia.runtime.run_state.RunState`.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable

if TYPE_CHECKING:
    from ..messages import Message
    from ..workspace.protocol import WorkspaceSession

from .. import events
from .checkpoint import CheckpointWriter
from .resume import IfRunExists, check_resumable, result_from_completed_snapshot
from .model_turn import stream_model_turn
from .run_state import ModelTurnResult, ResumeState, RunState
from .utils import (
    agent_model_label,
    input_preview,
    supports_json_schema,
    truncate_repr,
)
from .tool_calls import ToolCallProcessor
from ..agent import Agent
from ..approvals import ApprovalChannel
from ..checkpointer import Checkpointer, RunSnapshot
from ..context import CompactionRequest, Compaction, ContextPolicy, ContextResult
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
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
    entries_to_messages,
    messages_to_entries,
)
from ..messages import AssistantTurn, ToolCall, Usage
from ..output import (
    DefaultOutputRepair,
    StructuredOutput,
    format_output_instructions,
    loads_lenient,
    parse_structured_output,
    resolve_structured_output,
)
from ..providers.base import Provider
from ..reliability import CancelToken, RetryPolicy, RunBudget
from ..run_context import RunContext
from .result import RunResult
from ..session import Session
from ..tools import Tool
from ..tracing import NoopTracer, Span, Tracer

logger = logging.getLogger(__name__)

# Sentinel distinguishing "no final output yet" from a legitimate ``None``
# output (e.g. an Optional output_type).
_UNSET: object = object()


class RunLoop:
    """The event-producing async iterator behind :meth:`Runner.stream`.

    Construction wires up configuration; :meth:`stream` drives the run. All
    per-run mutable state lives in a :class:`RunState` created during
    bootstrap.
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
        if_run_exists: IfRunExists = "resume",
    ) -> None:
        if session is not None and session_id is None:
            raise UserError("session_id is required when session is provided")
        self.initial_agent = initial_agent
        self.user_input = user_input
        self.context = context
        self.session = session
        self.session_id = session_id
        self.max_turns = max_turns
        self.parent_usage = parent_usage
        self.budget = budget
        self.cancel_token = cancel_token
        self.retry = retry
        self.context_policy: ContextPolicy = context_policy or Compaction()
        self.run_id = run_id or (resume_from.run_id if resume_from else None)
        self.checkpointer = checkpointer
        # Resolved lazily in ``_resolve_resume``: a snapshot passed in directly,
        # or one loaded by ``run_id`` per the ``if_run_exists`` policy.
        self.resume_from = resume_from
        self.if_run_exists = if_run_exists
        self.append_instructions = append_instructions
        # ``output_type=None`` means "use the active agent's output_type";
        # any other value is a run-wide final-output contract.
        self.output_type_override = output_type_override
        self.approvals = ApprovalChannel()
        self.checkpoints = CheckpointWriter(
            checkpointer=checkpointer,
            run_id=self.run_id,
            delete_on_success=delete_checkpoint_on_success,
        )

    # ------------------------------------------------------------------ #
    # Stream driver
    # ------------------------------------------------------------------ #

    async def stream(self) -> AsyncIterator[events.Event]:
        agent = self.initial_agent
        tracer: Tracer = agent.tracer or NoopTracer()

        with tracer.span("run", agent=agent.name, run_id=self.run_id) as run_span:
            async for ev in self._stream_inner(tracer, run_span):
                yield ev

    async def _stream_inner(
        self, tracer: Tracer, run_span: Span
    ) -> AsyncIterator[events.Event]:
        async with AsyncExitStack() as resources:
            completed = await self._resolve_resume()
            run_span.set_attribute(
                "resumed", completed is not None or self.resume_from is not None
            )
            if completed is not None:
                # Already-completed run: replay terminal events only. No
                # bootstrap, guardrails, or hooks — those ran on the original
                # completion; replay just folds usage and clears the checkpoint.
                if self.parent_usage is not None:
                    self.parent_usage.add(completed.usage)
                if self.checkpoints.delete_on_success:
                    await self.checkpoints.delete()
                yield events.RunStarted(agent=self.initial_agent)
                yield events.RunCompleted(result=completed)
                return

            state = await self._bootstrap(resources)
            processor = ToolCallProcessor(
                approvals=self.approvals,
                cancel_token=self.cancel_token,
                budget=self.budget,
            )

            yield await self._emit(state, events.RunStarted(agent=state.agent))
            logger.info(
                "run.start: agent=%r model=%s input=%s",
                state.agent.name,
                agent_model_label(state.agent),
                truncate_repr(input_preview(self.user_input)),
            )

            run_completed = False
            turn_durable = True
            try:
                # Input guardrails run once on the fully-built initial
                # transcript. Skip on resume — they already ran on the
                # original input.
                if state.agent.input_guardrails and self.resume_from is None:
                    await check_input_guardrails(
                        state.agent.input_guardrails,
                        entries_to_messages(state.transcript),
                        state.run_ctx,
                    )

                if self.resume_from is not None:
                    async for ev in self._drain_pending_calls(
                        state, processor, resources, tracer
                    ):
                        yield ev

                output: object = _UNSET
                while output is _UNSET:
                    self._check_limits(state)
                    state.turns += 1
                    yield await self._emit(
                        state, events.TurnStarted(agent=state.agent, turn=state.turns)
                    )
                    logger.debug(
                        "run.turn.start: agent=%r turn=%d",
                        state.agent.name,
                        state.turns,
                    )

                    turn_durable = False
                    turn = ModelTurnResult()
                    async for ev in self._model_phase(state, turn, tracer):
                        yield ev
                    assistant = turn.assistant
                    if assistant is None:
                        # Provider exited without emitting ``done`` — shouldn't
                        # happen for well-behaved adapters, but be defensive.
                        raise RuntimeError(
                            "Provider stream ended without final message"
                        )
                    self._record_usage(state, assistant)
                    state.transcript.extend(turn.turn_entries)
                    yield await self._emit(
                        state, events.MessageCompleted(entries=turn.turn_entries)
                    )
                    # Persist requested tool calls before executing them, so a
                    # crash mid-execution can resume by draining the calls
                    # that have no matching result yet.
                    await self.checkpoints.save_running(state)
                    turn_durable = True

                    if assistant.tool_calls:
                        async for ev in self._tool_phase(
                            state, processor, assistant.tool_calls, tracer
                        ):
                            yield ev
                    else:
                        output = await self._finalize_output(state, assistant)

                    yield await self._emit(
                        state, events.TurnEnded(agent=state.agent, turn=state.turns)
                    )
                    if state.pending_handoff is not None:
                        async for ev in self._apply_handoff(state, resources, tracer):
                            yield ev
                    await self.checkpoints.save_running(state)

                result = await self._finalize_run(state, output, run_span)
                await self.checkpoints.complete(state, result.output)
                run_completed = True

                yield await self._emit(state, events.RunCompleted(result=result))
                logger.info(
                    "run.done: agent=%r turns=%d tokens=%d(in=%d out=%d)",
                    result.final_agent.name,
                    result.turns,
                    result.usage.total_tokens,
                    result.usage.input_tokens,
                    result.usage.output_tokens,
                )
            except GeneratorExit:
                # The consumer abandoned the stream; not a run failure.
                raise
            except BaseException as exc:
                if not run_completed:
                    if not turn_durable:
                        # The in-flight turn left nothing in the transcript.
                        state.turns = max(0, state.turns - 1)
                    # Shield the terminal save: a run cancelled via
                    # wait_for/timeout must still leave an ``interrupted``
                    # snapshot. Without the shield, awaiting here could itself
                    # be cancelled and drop the checkpoint.
                    await asyncio.shield(self.checkpoints.save_terminal(state, exc))
                if isinstance(exc, Exception):
                    yield await self._emit(state, events.ErrorOccurred(error=exc))
                raise

    # ------------------------------------------------------------------ #
    # Phases
    # ------------------------------------------------------------------ #

    async def _resolve_resume(self) -> RunResult | None:
        """Apply the ``if_run_exists`` policy, loading the snapshot by ``run_id``.

        Returns a :class:`RunResult` when the target run already ``completed``
        (the caller replays it); otherwise returns ``None`` and, for a resumable
        snapshot, sets ``self.resume_from`` so :meth:`_bootstrap` rehydrates it.
        Raises :class:`UserError` for an unresumable snapshot or a policy
        conflict (``require`` with nothing stored, or ``fail`` with a run already
        present).
        """
        snapshot = self.resume_from
        if snapshot is None:
            if self.checkpointer is None or self.run_id is None:
                return None
            if self.if_run_exists == "restart":
                return None  # ignore any stored run and start fresh
            snapshot = await self.checkpointer.load(self.run_id)

        if snapshot is None:
            if self.if_run_exists == "require":
                raise UserError(f"No snapshot found for run_id={self.run_id!r}")
            return None  # nothing stored yet — start fresh

        if self.if_run_exists == "fail":
            raise UserError(
                f"A run already exists for run_id={self.run_id!r} "
                f"(status={snapshot.status!r}).",
                hint="Pass if_run_exists='resume' to continue it, or 'restart' to overwrite it.",
            )

        check_resumable(
            self.initial_agent, snapshot, output_type=self.output_type_override
        )
        if snapshot.status == "completed":
            return result_from_completed_snapshot(
                self.initial_agent, snapshot, output_type=self.output_type_override
            )
        self.resume_from = snapshot
        return None

    async def _bootstrap(self, resources: AsyncExitStack) -> RunState:
        """Connect tools, build the initial transcript, and assemble RunState."""
        agent = self.initial_agent
        resume_state = (
            ResumeState.from_dict(self.resume_from.resume_state)
            if self.resume_from is not None
            else ResumeState()
        )
        providers = self._resolve_providers(agent, resources)
        structured_output = resolve_structured_output(
            self._resolve_output_type(agent, resume_state),
            supports_json_schema(providers),
        )
        system_extra = self.append_instructions

        mcp_tools = await self._connect_mcp(agent, resources)
        workspace, workspace_tools = await self._connect_workspace(agent, resources)
        tools_by_name = self._collect_tools(agent, mcp_tools, workspace_tools)

        if self.resume_from is not None:
            transcript: list[TranscriptEntry] = list(self.resume_from.entries)
        else:
            transcript = await self._build_initial_entries(
                agent, structured_output, system_extra
            )

        run_ctx = RunContext(
            context=self.context,
            entries=transcript,
            agent=agent,
            session_id=self.session_id,
            workspace=workspace,
        )
        if self.resume_from is not None:
            run_ctx.usage.add(self.resume_from.usage)

        return RunState(
            run_ctx=run_ctx,
            tools_by_name=tools_by_name,
            structured_output=structured_output,
            resume_state=resume_state,
            providers=providers,
            turns=self.resume_from.turns if self.resume_from is not None else 0,
            system_extra=system_extra,
        )

    async def _drain_pending_calls(
        self,
        state: RunState,
        processor: ToolCallProcessor,
        resources: AsyncExitStack,
        tracer: Tracer,
    ) -> AsyncIterator[events.Event]:
        """Execute tool calls a resumed snapshot left without results.

        The interrupted turn already streamed its model output in the
        original process, so this re-enters that turn (same ``turn`` number)
        for the tool-execution half only.
        """
        pending = pending_tool_calls(state.transcript)
        if not pending:
            return
        logger.info(
            "run.resume: draining %d pending tool call(s) for turn %d",
            len(pending),
            state.turns,
        )
        yield await self._emit(
            state, events.TurnStarted(agent=state.agent, turn=state.turns)
        )
        async for ev in self._tool_phase(state, processor, pending, tracer):
            yield ev
        yield await self._emit(
            state, events.TurnEnded(agent=state.agent, turn=state.turns)
        )
        if state.pending_handoff is not None:
            async for ev in self._apply_handoff(state, resources, tracer):
                yield ev
        await self.checkpoints.save_running(state)

    async def _model_phase(
        self, state: RunState, turn: ModelTurnResult, tracer: Tracer
    ) -> AsyncIterator[events.Event]:
        """Run one model call against the context policy's view of the transcript.

        The view is per-call only: ``state.transcript`` (and the Session) are
        never modified by compaction. When the provider reports a context
        overflow before any output reached the consumer, the policy gets one
        chance to produce a more aggressive view and the call is retried; a
        second overflow — or one after partial output — propagates.
        """
        providers = state.providers
        primary = providers[0]
        request = CompactionRequest(
            entries=state.transcript,
            provider=primary,
            model=getattr(primary, "model", None),
            last_input_tokens=state.resume_state.last_input_tokens,
            session_id=self.session_id,
            run_id=self.run_id,
            overflow=False,
            scratch=state.resume_state.compaction_scratch,
            workspace=state.run_ctx.workspace,
            tool_names=frozenset(state.tools_by_name),
        )
        ctx_result = await self.context_policy.compact(request)
        view = await self._build_view(state, ctx_result)
        if ctx_result.compacted:
            yield await self._emit(
                state, self._compacted_event(state, view, ctx_result, reactive=False)
            )

        forwarded = False
        try:
            async for ev in self._call_model(state, providers, view, turn, tracer):
                forwarded = True
                yield ev
            return
        except ContextOverflowError as overflow:
            if forwarded:
                # Partial output already reached the consumer; retrying the
                # turn would stream it again.
                raise
            logger.warning(
                "context.overflow: provider raised; rebuilding a more "
                "aggressive view (%s)",
                overflow,
            )
            request.overflow = True
            ctx_result = await self.context_policy.compact(request)
            if not ctx_result.compacted:
                logger.error(
                    "context.overflow: policy could not shrink transcript; "
                    "surfacing ContextOverflowError"
                )
                raise
            view = await self._build_view(state, ctx_result)

        yield await self._emit(
            state, self._compacted_event(state, view, ctx_result, reactive=True)
        )
        turn.assistant = None
        turn.turn_entries = []
        async for ev in self._call_model(state, providers, view, turn, tracer):
            yield ev

    async def _call_model(
        self,
        state: RunState,
        providers: list[Provider],
        view: list[TranscriptEntry],
        turn: ModelTurnResult,
        tracer: Tracer,
    ) -> AsyncIterator[events.Event]:
        async for ev in stream_model_turn(
            agent=state.agent,
            providers=providers,
            input_entries=view,
            tools_by_name=state.tools_by_name,
            structured_output=state.structured_output,
            tracer=tracer,
            turn=state.turns,
            result=turn,
            retry=self.retry,
        ):
            yield await self._emit(state, ev)

    async def _tool_phase(
        self,
        state: RunState,
        processor: ToolCallProcessor,
        calls: list[ToolCall],
        tracer: Tracer,
    ) -> AsyncIterator[events.Event]:
        for call in calls:
            async for ev in processor.process(call, state=state, tracer=tracer):
                yield await self._emit(state, ev)
            await self.checkpoints.save_running(state)

    async def _apply_handoff(
        self, state: RunState, resources: AsyncExitStack, tracer: Tracer
    ) -> AsyncIterator[events.Event]:
        """Switch the active agent in place and rebuild agent-specific state.

        The new agent gets its own MCP/workspace connections and tool set;
        the previous agent's run-scoped connections stay open until the run
        ends (closing them eagerly would add failure modes for no gain).
        """
        signal = state.pending_handoff
        assert signal is not None
        state.pending_handoff = None
        prev_agent = state.agent
        target = signal.target
        logger.info("run.handoff: %r → %r", prev_agent.name, target.name)
        with tracer.span("handoff", from_agent=prev_agent.name, to_agent=target.name):
            state.agent = target
            # The per-call addendum applies to the initial agent only.
            state.system_extra = None
            state.providers = self._resolve_providers(target, resources)
            state.structured_output = resolve_structured_output(
                self._resolve_output_type(target, state.resume_state),
                supports_json_schema(state.providers),
            )
            mcp_tools = await self._connect_mcp(target, resources)
            workspace, workspace_tools = await self._connect_workspace(
                target, resources
            )
            state.run_ctx.workspace = workspace
            state.tools_by_name = self._collect_tools(
                target, mcp_tools, workspace_tools
            )
            await self._reset_transcript_for_handoff(state, signal.handoff)

        ev = events.HandoffOccurred(from_agent=prev_agent, to_agent=target)
        if prev_agent.hooks is not None and prev_agent.hooks is not target.hooks:
            await dispatch(prev_agent.hooks, ev)
        yield await self._emit(state, ev)

    async def _finalize_output(
        self, state: RunState, assistant: AssistantTurn
    ) -> object:
        """Parse the final assistant message, or arm one output-repair retry.

        Returns the run output, or :data:`_UNSET` after appending a repair
        prompt so the loop rolls another turn.
        """
        try:
            return self._parse_output(state, assistant.content or "")
        except OutputValidationError as exc:
            attempt = state.resume_state.output_repair_attempts + 1
            repair_prompt = self._build_repair_prompt(state.agent, exc, attempt)
            if repair_prompt is None or state.structured_output is None:
                raise
            state.resume_state.output_repair_attempts = attempt
            logger.warning(
                "run.output_repair: agent=%r attempt=%d schema=%s error=%s",
                state.agent.name,
                attempt,
                exc.output_type_name,
                truncate_repr(str(exc)),
            )
            state.transcript.append(InputEntry(role="user", content=repair_prompt))
            return _UNSET

    async def _finalize_run(
        self, state: RunState, output: object, run_span: Span
    ) -> RunResult:
        """Run output guardrails, persistence, and usage propagation."""
        if state.agent.output_guardrails:
            await check_output_guardrails(
                state.agent.output_guardrails, output, state.run_ctx
            )

        result = RunResult(
            output=output,
            entries=state.transcript,
            final_agent=state.agent,
            usage=state.run_ctx.usage,
            turns=state.turns,
        )

        if self.session is not None:
            await self._persist_session(state.transcript)

        if self.parent_usage is not None:
            self.parent_usage.add(state.run_ctx.usage)

        run_span.set_attribute("turns", state.turns)
        run_span.set_attribute("total_tokens", state.run_ctx.usage.total_tokens)
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _emit(self, state: RunState, ev: events.Event) -> events.Event:
        """Dispatch ``ev`` to the active agent's hooks, then hand it back to
        be yielded to the stream consumer (``yield await self._emit(...)``)."""
        await dispatch(state.agent.hooks, ev)
        return ev

    def _check_limits(self, state: RunState) -> None:
        if state.turns >= self.max_turns:
            logger.warning(
                "run.max_turns: agent=%r turns=%d/%d",
                state.agent.name,
                state.turns,
                self.max_turns,
            )
            raise MaxTurnsExceeded(
                f"Run exceeded max_turns={self.max_turns} without producing output"
            )
        if self.cancel_token is not None:
            self.cancel_token.check()
        if self.budget is not None:
            self.budget.check(state.run_ctx.usage)

    def _record_usage(self, state: RunState, assistant: AssistantTurn) -> None:
        state.run_ctx.usage.add(assistant.usage)
        # Remember the real input-token count so the next turn's
        # ContextPolicy can size compaction against actual usage rather
        # than the chars/4 heuristic.
        if assistant.usage and assistant.usage.input_tokens:
            state.resume_state.last_input_tokens = assistant.usage.input_tokens
        if self.budget is not None:
            self.budget.check(state.run_ctx.usage)

    def _resolve_output_type(self, agent: Agent, resume_state: ResumeState) -> object:
        """Return the output type to use for ``agent``.

        A Runner-level override is a run-wide final-output contract. When no
        override was supplied, each active agent uses its declared
        ``output_type``.
        """
        if self.output_type_override is not None:
            resume_state.output_type_source = "run_override"
            return self.output_type_override
        resume_state.output_type_source = "agent"
        return agent.output_type

    def _parse_output(self, state: RunState, content: str) -> object:
        if state.structured_output is None:
            return content
        # The model either used the native ``response_format`` path or was
        # instructed via the system prompt to reply with schema-shaped JSON.
        # Either way the final text must parse as JSON describing the target
        # type; failures surface as ``OutputValidationError`` and may be
        # repaired in the main loop if the agent opts in.
        try:
            return parse_structured_output(
                state.structured_output, loads_lenient(content)
            )
        except OutputValidationError as exc:
            if exc.output_type_name is None:
                exc.output_type_name = getattr(
                    state.structured_output.output_type,
                    "__name__",
                    str(state.structured_output.output_type),
                )
            raise

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

    def _system_entry(self, system_text: str) -> list[TranscriptEntry]:
        """Wrap rendered system text as a leading entry (``[]`` when blank)."""
        return [InputEntry(role="system", content=system_text)] if system_text else []

    async def _build_initial_entries(
        self,
        agent: Agent,
        structured_output: StructuredOutput | None,
        system_extra: str | None,
    ) -> list[TranscriptEntry]:
        entries: list[TranscriptEntry] = []
        system_text = await self._system_prompt(
            agent, structured_output, extra=system_extra
        )
        entries.extend(self._system_entry(system_text))

        if self.session is not None:
            assert self.session_id is not None  # validated in __init__
            entries.extend(await self.session.load(self.session_id))

        if isinstance(self.user_input, str):
            entries.append(InputEntry(role="user", content=self.user_input))
        else:
            entries.extend(messages_to_entries(self.user_input))
        return entries

    async def _system_prompt(
        self,
        agent: Agent,
        structured_output: StructuredOutput | None,
        *,
        extra: "str | None" = None,
    ) -> str:
        """Render the full system prompt for ``agent``.

        Concatenates the agent's instructions (plus the optional per-run
        ``extra`` addendum), workspace and skills instructions, and — for
        providers without native ``response_format`` support — the
        structured-output contract.
        """
        parts = [await agent.render_instructions(self.context, extra=extra)]
        if agent.workspace is not None:
            parts.append(agent.workspace.instructions())
        if agent.skills is not None:
            parts.append(agent.skills.instructions())
        if structured_output is not None and not structured_output.use_native:
            parts.append(format_output_instructions(structured_output))
        return "\n\n".join(part for part in parts if part).strip()

    async def _reset_transcript_for_handoff(
        self, state: RunState, handoff: Handoff | None
    ) -> None:
        """Swap the leading system message for the new active agent.

        Operates on entries directly so nothing is lost in translation. Only
        when the originating :class:`Handoff` declares an ``input_filter`` is
        the body round-tripped through the (lossy, message-shaped) filter API.
        """
        new_system = await self._system_prompt(
            state.agent, state.structured_output, extra=state.system_extra
        )
        body: list[TranscriptEntry] = list(state.transcript)
        if body and isinstance(body[0], InputEntry) and body[0].role == "system":
            body = body[1:]
        if handoff is not None and handoff.input_filter is not None:
            body = messages_to_entries(
                list(handoff.input_filter(entries_to_messages(body)))
            )
        head = self._system_entry(new_system)
        # In-place so RunContext.entries keeps observing the same list.
        state.transcript[:] = [*head, *body]

    def _collect_tools(
        self,
        agent: Agent,
        mcp_tools: list[Tool],
        workspace_tools: list[Tool],
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
        for t in workspace_tools:
            add_tool("agent.workspace", t)
        for t in mcp_tools:
            add_tool("mcp", t)
        for h in agent.handoffs:
            handoff_obj = h if isinstance(h, Handoff) else Handoff(target=h)
            add_tool("handoff", build_handoff_tool(handoff_obj))
        if agent.skills is not None:
            for t in agent.skills.tools():
                add_tool("skills", t)
        return tools

    def _resolve_providers(
        self, agent: Agent, resources: AsyncExitStack
    ) -> list[Provider]:
        """Resolve ``agent``'s provider chain once for the rest of the run.

        Providers built here from string specs are owned by the run: their
        lazily-created HTTP clients are reused across turns and closed when
        the run ends. User-supplied :class:`Provider` instances are never
        closed — their lifecycle belongs to the caller.
        """
        specs = agent.model if isinstance(agent.model, list) else [agent.model]
        providers = agent.resolve_providers()
        # NOTE: this pairs each provider with its spec positionally, which is
        # correct only while Agent.resolve_providers() returns providers 1:1 in
        # agent.model order. If it ever dedups or reorders, the run-owned (built
        # from a string spec) vs caller-owned distinction below would be wrong.
        for spec, provider in zip(specs, providers):
            if isinstance(spec, str):
                aclose = getattr(provider, "aclose", None)
                if callable(aclose):
                    _push_cleanup(resources, aclose)
        return providers

    async def _connect_mcp(self, agent: Agent, resources: AsyncExitStack) -> list[Tool]:
        tools: list[Tool] = []
        for server in agent.mcp_servers:
            conn = await server.open()
            if server.close_after_run:
                _push_cleanup(resources, conn.close)
            tools.extend(conn.tools())
        return tools

    async def _connect_workspace(
        self, agent: Agent, resources: AsyncExitStack
    ) -> "tuple[WorkspaceSession | None, list[Tool]]":
        """Open the agent's workspace and return its session and tool bundle.

        The session is also injected into ``RunContext.workspace`` by the
        caller, which is where the built-in file/shell tools find it.
        """
        if agent.workspace is None:
            return None, []
        session = await agent.workspace.open()
        if agent.workspace.close_after_run:
            _push_cleanup(resources, session.close)
        return session, agent.workspace.tools()

    async def _persist_session(self, transcript: list[TranscriptEntry]) -> None:
        # Replace stored transcript with the latest (simple and predictable).
        # System prompts are agent-owned and re-rendered each run, so any
        # system :class:`InputEntry` is excluded from the persisted history.
        assert self.session is not None and self.session_id is not None
        body = [
            entry
            for entry in transcript
            if not (isinstance(entry, InputEntry) and entry.role == "system")
        ]
        await self.session.replace(self.session_id, body)

    async def _build_view(
        self, state: RunState, result: ContextResult
    ) -> list[TranscriptEntry]:
        """Return the per-call view to send to the provider.

        Compaction is view-only: ``state.transcript`` is never mutated. When
        the policy dropped the leading system message (e.g. it summarized the
        head), re-prepend it so provider adapters still see one.
        """
        if not result.changed:
            return state.transcript
        view = result.entries
        if view and isinstance(view[0], InputEntry) and view[0].role == "system":
            return view
        system_text = await self._system_prompt(
            state.agent, state.structured_output, extra=state.system_extra
        )
        return [*self._system_entry(system_text), *view]

    def _compacted_event(
        self,
        state: RunState,
        view: list[TranscriptEntry],
        result: ContextResult,
        *,
        reactive: bool,
    ) -> events.ContextCompacted:
        metadata = dict(result.metadata)
        if result.tokens_before is not None:
            metadata["tokens_before"] = result.tokens_before
        if result.tokens_after is not None:
            metadata["tokens_after"] = result.tokens_after
        return events.ContextCompacted(
            session_id=self.session_id,
            entries_before=list(state.transcript),
            entries_after=list(view),
            summary=result.summary,
            reactive=reactive,
            reason=result.reason or "context_policy",
            metadata=metadata,
        )


def pending_tool_calls(entries: list[TranscriptEntry]) -> list[ToolCall]:
    """Tool calls in ``entries`` that have no matching result yet.

    Duplicate call ids are paired by occurrence order: each result consumes
    one earlier call with the same id.
    """
    unconsumed_results = Counter(
        entry.call_id for entry in entries if isinstance(entry, ToolResultEntry)
    )
    pending: list[ToolCall] = []
    for entry in entries:
        if not isinstance(entry, ToolCallEntry):
            continue
        if unconsumed_results[entry.call_id] > 0:
            unconsumed_results[entry.call_id] -= 1
        else:
            pending.append(
                ToolCall(id=entry.call_id, name=entry.name, arguments=entry.arguments)
            )
    return pending


def _push_cleanup(
    resources: AsyncExitStack, close: Callable[[], Awaitable[None]]
) -> None:
    """Register ``close`` for best-effort teardown when the run ends."""

    async def safe_close() -> None:
        try:
            await close()
        except Exception:
            logger.debug("run.cleanup: connection close failed", exc_info=True)

    resources.push_async_callback(safe_close)


__all__ = ["RunLoop"]
