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
import inspect
import logging
import time
from collections import Counter
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

if TYPE_CHECKING:
    from ..messages import Message
    from ..workspace.protocol import WorkspaceSession

from .. import events
from .checkpoint import CheckpointWriter
from .resume import resolve_resume_agent, result_from_completed_snapshot
from .model_turn import stream_model_turn
from .run_state import ActiveAgent, ModelTurnResult, PluginActivation, RunState
from .utils import (
    agent_model_label,
    input_preview,
    supports_json_schema,
    truncate_repr,
)
from .tool_calls import ToolCallProcessor
from ..agent import Agent
from ..approvals import ApprovalChannel
from ..checkpointer import CheckpointOptions
from ..context import CompactionRequest, Compaction, ContextPolicy, ContextResult
from ..exceptions import (
    ContextOverflowError,
    MaxTurnsExceeded,
    OutputValidationError,
    UserError,
)
from ..guardrails import (
    check_input_guardrails,
    check_output_guardrails,
)
from ..handoff import Handoff, build_handoff_tool
from ..hooks import dispatch
from ..transcript import (
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
    entries_to_messages,
    leading_system_count,
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
from ..session import Segment, Session
from ..tools import Tool
from ..tracing import NoopTracer, Span, Tracer, handoff_span, record_run_end, run_span

logger = logging.getLogger(__name__)

# Sentinel distinguishing "no final output yet" from a legitimate ``None``
# output (e.g. an Optional output_type).
_UNSET: object = object()

# Reserved key under which a context policy's cross-run carryover rides in a
# finished run's session-segment ``meta``. Namespaced so ``meta`` can host
# other co-tenants; the value is opaque to the loop (it just round-trips it
# into the next run's ``context_policy_state``).
CARRYOVER_META_KEY = "context_carryover"


class RunLoop:
    """The event-producing async iterator behind :meth:`Runner.stream`.

    Construction wires up configuration; :meth:`stream` drives the run. All
    per-run mutable state lives in a :class:`RunState` created during
    bootstrap.
    """

    def __init__(
        self,
        *,
        initial_agent: Agent[Any],
        user_input: "str | list[Message]",
        context: object,
        output_type_override: object | None = None,
        extra_instructions: "str | None" = None,
        max_turns: int,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        context_policy: ContextPolicy | None = None,
        session: Session | None,
        session_id: str | None,
        checkpoint: CheckpointOptions | None = None,
        tracer: Tracer | None = None,
        parent_usage: Usage | None = None,
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
        # Always hold a token so it can be exposed on RunContext (for tools and
        # hooks to call cancel()) and inherited by agent-as-tool sub-runs. When
        # the caller didn't pass one, a fresh token is never cancelled, so this
        # is behaviourally identical to the old None for callers who don't reach
        # for it.
        self.cancel_token = cancel_token or CancelToken()
        # Run-scoped observability. ``None`` → NoopTracer at stream() time, so
        # instrumentation stays free. A run-level knob (like budget/cancel_token),
        # not a per-agent one: it applies across handoffs to whatever agent is
        # active, which a field on the initial agent could not express.
        self.tracer = tracer
        self.retry = retry
        self.context_policy: ContextPolicy = context_policy or Compaction(
            context_window=200_000
        )
        self.run_id = checkpoint.resolved_run_id if checkpoint is not None else None
        self.checkpointer = checkpoint.checkpointer if checkpoint is not None else None
        # Resolved lazily in ``_resolve_resume``: a snapshot passed in directly,
        # or one loaded by ``run_id`` per the ``if_run_exists`` policy.
        self.resume_from = checkpoint.resume_from if checkpoint is not None else None
        # The active agent to resume as, resolved from ``initial_agent``'s
        # handoff graph by ``_resolve_resume`` (the snapshot's agent may be a
        # handoff target, not the entry agent). ``None`` for a fresh run.
        self._resume_agent: Agent[Any] | None = None
        self.if_run_exists = (
            checkpoint.if_run_exists if checkpoint is not None else "resume"
        )
        self.extra_instructions = extra_instructions
        # ``output_type=None`` means "use the active agent's output_type";
        # any other value is a run-wide final-output contract.
        self.output_type_override = output_type_override
        self.approvals = ApprovalChannel()
        self.checkpoints = CheckpointWriter(
            checkpointer=self.checkpointer,
            run_id=self.run_id,
            delete_on_success=(
                checkpoint.delete_on_success if checkpoint is not None else False
            ),
        )

    # ------------------------------------------------------------------ #
    # Stream driver
    # ------------------------------------------------------------------ #

    async def stream(self) -> AsyncIterator[events.Event]:
        agent = self.initial_agent
        tracer: Tracer = self.tracer or NoopTracer()

        with run_span(tracer, agent=agent.name, run_id=self.run_id or "") as span:
            async for ev in self._stream_inner(tracer, span):
                yield ev

    async def _stream_inner(
        self, tracer: Tracer, span: Span
    ) -> AsyncIterator[events.Event]:
        async with AsyncExitStack() as resources:
            completed = await self._resolve_resume()
            span.set_attribute(
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
                # Quote the free-text input so its spaces/"=" don't read as fields.
                "run.start: agent=%r model=%s input='%s'",
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
                input_guardrails = (
                    state.agent.input_guardrails
                    + state.active.plugins.input_guardrails
                )
                if input_guardrails and self.resume_from is None:
                    await check_input_guardrails(
                        input_guardrails,
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
                    # Mirror the turn counter onto the public context so a tool
                    # or hook can tell which step of the loop it is running in.
                    state.run_ctx.turn = state.turns
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
                    # Bracket the model call the way tool.start/tool.done bracket a
                    # tool, so a slow or hung provider is visible at INFO — not
                    # only after it returns.
                    logger.info(
                        "model.start: turn=%d model=%s",
                        state.turns,
                        agent_model_label(state.agent),
                    )
                    model_started = time.perf_counter()
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
                    logger.info(
                        "model.done: turn=%d tokens=%d(in=%d out=%d) finish=%s "
                        "tool_calls=%d dur=%.2fs",
                        state.turns,
                        assistant.usage.total_tokens,
                        assistant.usage.input_tokens,
                        assistant.usage.output_tokens,
                        assistant.finish_reason,
                        len(assistant.tool_calls),
                        time.perf_counter() - model_started,
                    )
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

                result = await self._finalize_run(state, output, span)
                await self.checkpoints.complete(state, result.output)
                run_completed = True
                # Append to the Session only AFTER the checkpoint is finalized.
                # Resume reloads history from the Session, so a run that is both
                # persisted there AND still resumable would double-count on
                # resume. Finalizing the checkpoint first keeps the run in
                # exactly one place; ``run_completed`` is already set, so a
                # failure here can't un-complete the checkpoint (no save_terminal).
                if self.session is not None:
                    await self._persist_session(state)

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
                    # Symmetric with run.done: a run that ends by exception is
                    # logged too, keyed off the same classifier the checkpoint
                    # uses so the level matches the snapshot status. No exc_info —
                    # the exception is re-raised below, so the caller still gets
                    # the traceback; this just adds the run-scoped summary it lacks.
                    status = self.checkpoints.classify(exc)
                    emit = logger.warning if status == "interrupted" else logger.error
                    emit(
                        "run.%s: agent=%r turn=%d (%s: %s)",
                        status,
                        state.agent.name,
                        state.turns,
                        type(exc).__name__,
                        truncate_repr(str(exc)),
                    )
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
        conflict (``resume_only`` with nothing stored, or ``fail`` with a run already
        present).
        """
        snapshot = self.resume_from
        if snapshot is None:
            if self.checkpointer is None or self.run_id is None:
                return None
            if self.if_run_exists == "restart":
                # Discard the stored run's rows so fresh turns don't append to
                # them, then start fresh.
                await self.checkpoints.delete()
                return None
            snapshot = await self.checkpointer.load(self.run_id)

        if snapshot is None:
            if self.if_run_exists == "resume_only":
                raise UserError(f"No snapshot found for run_id={self.run_id!r}")
            return None  # nothing stored yet — start fresh

        if self.if_run_exists == "fail":
            raise UserError(
                f"A run already exists for run_id={self.run_id!r} "
                f"(status={snapshot.status!r}).",
                hint=(
                    "Use CheckpointOptions(..., if_run_exists='resume') to "
                    "continue it, or 'restart' to overwrite it."
                ),
            )

        active_agent = resolve_resume_agent(self.initial_agent, snapshot)
        if snapshot.status == "completed":
            return result_from_completed_snapshot(
                active_agent, snapshot, output_type=self.output_type_override
            )
        self.resume_from = snapshot
        self._resume_agent = active_agent
        return None

    async def _bootstrap(self, resources: AsyncExitStack) -> RunState:
        """Resolve the active agent, build the initial transcript, assemble RunState.

        On resume the active agent is the one recorded in the snapshot — which
        may be a handoff target rather than the entry agent; ``_resolve_resume``
        resolved it from the entry agent's handoff graph. A fresh run starts on
        the entry agent.
        """
        snapshot = self.resume_from
        if snapshot is not None:
            assert self._resume_agent is not None  # set by _resolve_resume
            agent = self._resume_agent
        else:
            agent = self.initial_agent
        active = await self._resolve_active(agent, resources)
        extra_instructions = self.extra_instructions

        # Build the context first (with an empty transcript) so the initial
        # system prompt can be rendered against the live ``run_ctx`` — dynamic
        # instruction fragments then see the same handle tools/hooks receive.
        run_ctx: RunContext[Any] = RunContext(
            context=self.context,
            entries=[],
            agent=active.agent,
            session_id=self.session_id,
            run_id=self.run_id,
            budget=self.budget,
            workspace=active.workspace,
            cancel_token=self.cancel_token,
        )
        # Read prior session history once, as segments: the flattened entries
        # become the prefix, and the latest segment's ``meta`` seeds a fresh
        # run's context-policy carryover (below).
        segments: list[Segment] = []
        if self.session is not None:
            assert self.session_id is not None  # validated in __init__
            segments = await self.session.segments(self.session_id)
        history = [entry for seg in segments for entry in seg.entries]
        # The transcript is [system] + prior session history + this run's own
        # entries. ``run_start`` marks the boundary; on resume the run's own
        # entries come from the snapshot (the checkpoint stores only those), so
        # we rebuild the system/history prefix fresh and append them.
        prefix = await self._build_prefix(
            active.agent,
            active.structured_output,
            extra_instructions,
            active.plugins.instructions,
            run_ctx,
            history,
        )
        run_start = len(prefix)
        # How many leading entries (0 or 1) the runner's own system prompt
        # occupies. Tracked so a later handoff strips exactly the runner's head
        # and never a user-supplied leading ``system`` input entry (which a
        # systemless agent leaves at transcript[0]). ``prefix`` is head + history.
        system_head_len = len(prefix) - len(history)
        run_ctx.entries.extend(prefix)
        if snapshot is not None:
            run_ctx.entries.extend(snapshot.entries)
            run_ctx.usage.add(snapshot.usage)
            # These entries are already in the checkpoint; only persist new ones.
            self.checkpoints.resume_at(len(snapshot.entries))
        else:
            run_ctx.entries.extend(self._user_input_entries())

        # Seed the context policy's per-run scratch: the resuming run's own
        # checkpoint wins; otherwise a fresh run inherits the previous run's
        # carryover from the latest session segment's meta; else empty.
        if snapshot is not None:
            context_state = dict(snapshot.context_policy_state)
        elif segments:
            carryover = (segments[-1].meta or {}).get(CARRYOVER_META_KEY)
            context_state = dict(carryover) if isinstance(carryover, dict) else {}
        else:
            context_state = {}

        return RunState(
            run_ctx=run_ctx,
            active=active,
            run_start=run_start,
            system_head_len=system_head_len,
            turns=snapshot.turns if snapshot is not None else 0,
            extra_instructions=extra_instructions,
            last_input_tokens=(
                snapshot.last_input_tokens if snapshot is not None else None
            ),
            context_policy_state=context_state,
        )

    async def _resolve_active(
        self, agent: Agent[Any], resources: AsyncExitStack
    ) -> ActiveAgent:
        """Resolve everything derived from ``agent`` into one swappable bundle.

        Called at bootstrap and on every handoff. Providers, workspace, and
        plugin connections are run-scoped: they are opened here and torn down
        when the run ends (a handoff leaves the previous agent's connections
        open until then — closing them eagerly would add failure modes for no
        gain).
        """
        providers = self._resolve_providers(agent, resources)
        structured_output = resolve_structured_output(
            self._resolve_output_type(agent),
            supports_json_schema(providers),
        )
        workspace, workspace_tools = await self._connect_workspace(agent, resources)
        plugins = await self._activate_plugins(agent, resources)
        tools_by_name = self._collect_tools(agent, workspace_tools, plugins.tools)
        return ActiveAgent(
            agent=agent,
            providers=providers,
            structured_output=structured_output,
            tools_by_name=tools_by_name,
            workspace=workspace,
            plugins=plugins,
        )

    async def _activate_plugins(
        self, agent: Agent[Any], resources: AsyncExitStack
    ) -> PluginActivation:
        """Activate ``agent.plugins`` for one run, collecting their contributions.

        ``setup`` is awaited once per plugin so any run-scoped state (and async
        resources like MCP connections) is fresh and all of a plugin's
        contributions (tool, injector, ...) share it. Each instance's ``aclose``
        is registered for best-effort teardown when the run ends (LIFO).

        A plugin's ``name`` is its identity: it must be unique within an agent.
        Validated up front (before any ``setup``) so a duplicate is rejected
        without opening — then tearing down — a plugin's resources.
        """
        seen: set[str] = set()
        for plugin in agent.plugins:
            if plugin.name in seen:
                raise UserError(
                    f"Duplicate plugin name {plugin.name!r} on agent "
                    f"{agent.name!r}.",
                    hint="A plugin's name is its identity and must be unique "
                    "per agent; each is activated once per run. Remove the "
                    "duplicate or give one plugin a distinct name.",
                )
            seen.add(plugin.name)
        act = PluginActivation()
        for plugin in agent.plugins:
            inst = await plugin.setup()
            act.tools.extend(inst.tools)
            act.view_injectors.extend(inst.view_injectors)
            if inst.instructions:
                act.instructions.append(inst.instructions)
            if inst.hooks is not None:
                act.hooks.append(inst.hooks)
            act.input_guardrails.extend(inst.input_guardrails)
            act.output_guardrails.extend(inst.output_guardrails)
            _push_cleanup(resources, inst.aclose)
        return act

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
        providers = state.active.providers
        primary = providers[0]
        request = CompactionRequest(
            entries=state.transcript,
            provider=primary,
            model=getattr(primary, "model", None),
            last_input_tokens=state.last_input_tokens,
            overflow=False,
            scratch=state.context_policy_state,
        )
        ctx_result = await self.context_policy.compact(request)
        view = await self._build_view(state, ctx_result)
        if ctx_result.compacted:
            yield await self._emit(
                state, self._compacted_event(state, view, ctx_result, reactive=False)
            )
        view = await self._augment_view(state, view)

        forwarded = False
        try:
            async for ev in self._call_model(state, providers, view, turn, tracer):
                forwarded = True
                yield ev
            return
        except ContextOverflowError as overflow:
            if forwarded:
                # Partial output already reached the consumer; retrying the
                # turn would stream it again, so surface the overflow instead.
                logger.warning(
                    "context.overflow: provider raised after partial output "
                    "already streamed; cannot retry this turn (%s)",
                    overflow,
                )
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
        view = await self._augment_view(state, view)
        turn.assistant = None
        turn.turn_entries = []
        async for ev in self._call_model(state, providers, view, turn, tracer):
            yield ev

    async def _augment_view(
        self, state: RunState, view: list[TranscriptEntry]
    ) -> list[TranscriptEntry]:
        """Append transient per-turn entries from plugin view injectors.

        Injected entries are used for this one model call only — never added to
        ``state.transcript`` or the Session, so they don't accumulate as turns
        grow and don't bust the cached system-prompt prefix. A raising injector
        is logged and skipped (fail-open): a broken reminder must never abort a
        run. A fresh list is returned whenever anything is injected, so the live
        transcript is never mutated in place.

        Two consequences are deliberate, not bugs. (1) Because injected entries
        never enter the transcript, a resume from session/snapshot will not
        replay them — injectors are expected to regenerate their content each
        turn (reminders, clock, todo list), so this is by design. (2) Entries
        are appended after the context policy has already shaped ``view``, so
        very large injected content could push a turn over the window;
        injectors are meant to be small, and the provider's overflow path still
        applies.
        """
        if not state.active.plugins.view_injectors:
            return view
        injected: list[TranscriptEntry] = []
        for inject in state.active.plugins.view_injectors:
            try:
                result = inject(state.run_ctx)
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    injected.extend(result)
            except Exception:
                logger.warning("view injector failed; skipping", exc_info=True)
        if not injected:
            return view
        return [*view, *injected]

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
            tools_by_name=state.active.tools_by_name,
            structured_output=state.active.structured_output,
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
            # Persist after each tool result so a crash mid tool-execution can
            # resume by draining the calls that still have no matching result.
            await self.checkpoints.save_running(state)

    async def _apply_handoff(
        self, state: RunState, resources: AsyncExitStack, tracer: Tracer
    ) -> AsyncIterator[events.Event]:
        """Switch the active agent and rebuild its derived state as one unit.

        The new agent gets its own provider/workspace/plugin connections and
        tool set, bundled into a fresh :class:`ActiveAgent` and swapped in via
        :meth:`RunState.activate`. The previous agent's run-scoped connections
        stay open until the run ends (closing them eagerly would add failure
        modes for no gain). ``extra_instructions`` is run-scoped and carries
        over to the new agent.
        """
        signal = state.pending_handoff
        assert signal is not None
        state.pending_handoff = None
        prev_agent = state.agent
        target = signal.handoff.target
        logger.info("run.handoff: %r → %r", prev_agent.name, target.name)
        with handoff_span(tracer, from_agent=prev_agent.name, to_agent=target.name):
            state.activate(await self._resolve_active(target, resources))
            await self._reset_transcript_for_handoff(state)

        ev = events.HandoffOccurred(from_agent=prev_agent, to_agent=target)
        if prev_agent.hooks is not None and prev_agent.hooks is not target.hooks:
            await dispatch(prev_agent.hooks, ev, state.run_ctx)
        yield await self._emit(state, ev)

    async def _finalize_output(
        self, state: RunState, assistant: AssistantTurn
    ) -> object:
        """Parse the final assistant message, or arm one output-repair retry.

        Returns the run output, or :data:`_UNSET` after appending a repair
        prompt so the loop rolls another turn.
        """
        try:
            return self._parse_output(
                state.active.structured_output, assistant.content or ""
            )
        except OutputValidationError as exc:
            attempt = state.output_repair_attempts + 1
            repair_prompt = self._build_repair_prompt(state.agent, exc, attempt)
            if repair_prompt is None:
                raise
            state.output_repair_attempts = attempt
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
        self, state: RunState, output: object, span: Span
    ) -> RunResult:
        """Run output guardrails and usage propagation, and build the result.

        Session persistence is deliberately NOT done here — the loop appends to
        the Session only after the checkpoint is finalized (see ``_stream_inner``)
        so a crash between the two can't leave a run both persisted and resumable.
        """
        output_guardrails = (
            state.agent.output_guardrails + state.active.plugins.output_guardrails
        )
        if output_guardrails:
            await check_output_guardrails(output_guardrails, output, state.run_ctx)

        result = RunResult(
            output=output,
            entries=state.transcript,
            final_agent=state.agent,
            usage=state.run_ctx.usage,
            turns=state.turns,
        )

        if self.parent_usage is not None:
            self.parent_usage.add(state.run_ctx.usage)

        record_run_end(span, turns=state.turns, total_tokens=state.run_ctx.usage.total_tokens)
        return result

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    async def _emit(self, state: RunState, ev: events.Event) -> events.Event:
        """Dispatch ``ev`` to the active agent's hooks and any plugin hooks,
        then hand it back to be yielded to the stream consumer
        (``yield await self._emit(...)``)."""
        await dispatch(state.agent.hooks, ev, state.run_ctx)
        for hooks in state.active.plugins.hooks:
            await dispatch(hooks, ev, state.run_ctx)
        return ev

    def _check_limits(self, state: RunState) -> None:
        if state.turns >= self.max_turns:
            # No log here: the run.interrupted boundary log already records the
            # MaxTurnsExceeded (with run context), and the cancel/budget checks
            # below likewise just raise — keep the "why it ended" line in one
            # place instead of double-logging this case.
            raise MaxTurnsExceeded(
                f"Run exceeded max_turns={self.max_turns} without producing output"
            )
        self.cancel_token.check()
        if self.budget is not None:
            self.budget.check(state.run_ctx.usage)

    def _record_usage(self, state: RunState, assistant: AssistantTurn) -> None:
        state.run_ctx.usage.add(assistant.usage)
        # Remember the real input-token count so the next turn's ContextPolicy
        # can size compaction against actual usage rather than the chars/4
        # heuristic.
        if assistant.usage and assistant.usage.input_tokens:
            state.last_input_tokens = assistant.usage.input_tokens
        # Enforce the budget against this turn's tokens. The turn-start check
        # (``_check_limits``) ran before the model call, so on a final turn that
        # produces output without tool calls this is the only place an
        # output-token overrun is caught before the run completes.
        if self.budget is not None:
            self.budget.check(state.run_ctx.usage)

    def _resolve_output_type(self, agent: Agent[Any]) -> object:
        """Return the output type to use for ``agent``.

        A Runner-level override is a run-wide final-output contract. When no
        override was supplied, each active agent uses its declared
        ``output_type``.
        """
        if self.output_type_override is not None:
            return self.output_type_override
        return agent.output_type

    def _parse_output(
        self, structured_output: StructuredOutput | None, content: str
    ) -> object:
        if structured_output is None:
            return content
        # The model either used the native ``response_format`` path or was
        # instructed via the system prompt to reply with schema-shaped JSON.
        # Either way the final text must parse as JSON describing the target
        # type; failures surface as ``OutputValidationError`` and may be
        # repaired in the main loop if the agent opts in.
        try:
            return parse_structured_output(structured_output, loads_lenient(content))
        except OutputValidationError as exc:
            # ``loads_lenient`` raises before the target type is known, so name
            # it for the error message when the parse step didn't already.
            if exc.output_type_name is None:
                exc.output_type_name = getattr(
                    structured_output.output_type,
                    "__name__",
                    str(structured_output.output_type),
                )
            raise

    def _build_repair_prompt(
        self, agent: Agent[Any], exc: OutputValidationError, attempt: int
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

    async def _build_prefix(
        self,
        agent: Agent[Any],
        structured_output: StructuredOutput | None,
        system_extra: str | None,
        plugin_instructions: list[str] | None,
        run_ctx: "RunContext[Any]",
        history: list[TranscriptEntry],
    ) -> list[TranscriptEntry]:
        """The run's non-run prefix: the system entry plus prior session history.

        ``history`` is the flattened prior-session transcript, already fetched
        in ``_bootstrap`` from ``session.segments`` (one read serves both the
        prefix and the carryover meta). Everything appended after this prefix is
        the run's own contribution; its length is the run's
        :attr:`RunState.run_start` boundary.
        """
        entries: list[TranscriptEntry] = []
        system_text = await self._system_prompt(
            agent,
            structured_output,
            ctx=run_ctx,
            extra=system_extra,
            plugin_instructions=plugin_instructions,
        )
        entries.extend(self._system_entry(system_text))
        entries.extend(history)
        return entries

    def _user_input_entries(self) -> list[TranscriptEntry]:
        """This run's opening input, as transcript entries (the start of the run)."""
        if isinstance(self.user_input, str):
            return [InputEntry(role="user", content=self.user_input)]
        return list(messages_to_entries(self.user_input))

    async def _system_prompt(
        self,
        agent: Agent[Any],
        structured_output: StructuredOutput | None,
        *,
        ctx: "RunContext[Any]",
        extra: "str | None" = None,
        plugin_instructions: list[str] | None = None,
    ) -> str:
        """Render the full system prompt for ``agent``.

        Concatenates the agent's instructions (plus the optional per-run
        ``extra`` addendum), workspace, and plugin instructions, and —
        for providers without native ``response_format`` support — the
        structured-output contract. ``ctx`` is the run's :class:`RunContext`,
        forwarded to dynamic instruction fragments so they see the same handle
        tools and hooks receive.
        """
        parts = [await agent.render_system_prompt(ctx, extra=extra)]
        if agent.workspace is not None:
            parts.append(agent.workspace.instructions())
        for instructions in plugin_instructions or []:
            parts.append(instructions)
        if structured_output is not None and not structured_output.use_native:
            parts.append(format_output_instructions(structured_output))
        return "\n\n".join(part for part in parts if part).strip()

    async def _reset_transcript_for_handoff(self, state: RunState) -> None:
        """Swap the leading system message for the new active agent.

        Only the system entry changes: the new agent re-renders its own system
        prompt and the old one is dropped. The conversation body (prior history
        + this run's entries) is left intact, so the run's contribution stays
        contiguous at ``transcript[run_start:]`` and the Session/checkpoint keep
        full fidelity.
        """
        new_system = await self._system_prompt(
            state.agent,
            state.active.structured_output,
            ctx=state.run_ctx,
            extra=state.extra_instructions,
            plugin_instructions=state.active.plugins.instructions,
        )
        old_head_len = state.system_head_len
        body: list[TranscriptEntry] = state.transcript[old_head_len:]
        head = self._system_entry(new_system)
        # In-place so RunContext.entries keeps observing the same list.
        state.transcript[:] = [*head, *body]
        # Strip exactly the runner's old head (``system_head_len`` — tracked, not
        # inferred from transcript[0], which a systemless agent may leave at a
        # user-supplied ``system`` input entry) and prepend the new agent's.
        # ``run_start`` marks this run's first entry, just past the head + prior
        # history; the body is otherwise untouched, so the boundary shifts only
        # by the change in head length — usually 0, non-zero only when the two
        # agents differ in whether they render a system prompt (an agent with
        # empty ``instructions`` and no workspace/plugins/structured-output
        # renders none).
        state.run_start += len(head) - old_head_len
        state.system_head_len = len(head)

    def _collect_tools(
        self,
        agent: Agent[Any],
        workspace_tools: list[Tool],
        plugin_tools: list[Tool] | None = None,
    ) -> dict[str, Tool]:
        tools: dict[str, Tool] = {}

        def add_tool(source: str, t: Tool) -> None:
            if t.name in tools:
                raise UserError(
                    f"Tool name conflict for {t.name!r} from {source}.",
                    hint="Rename one tool or remove the duplicate. For MCP, set "
                    "MCPServer.name to prefix a server's tools "
                    "(e.g. name='fs' -> fs__read_file).",
                )
            tools[t.name] = t

        for t in agent.tools:
            add_tool("agent.tools", t)
        for t in plugin_tools or []:
            add_tool("plugin", t)
        for t in workspace_tools:
            add_tool("agent.workspace", t)
        for h in agent.handoffs:
            handoff_obj = h if isinstance(h, Handoff) else Handoff(target=h)
            add_tool("handoff", build_handoff_tool(handoff_obj))
        # Tools the context policy provides (e.g. recall_tool_result). Added
        # last and skipped on conflict, so an explicit tool of the same name
        # from any source above always wins.
        policy_tools = getattr(self.context_policy, "tools", None)
        for t in policy_tools() if callable(policy_tools) else []:
            if t.name in tools:
                logger.debug(
                    "context-policy tool %r shadowed by an explicit tool; skipping",
                    t.name,
                )
                continue
            tools[t.name] = t
        return tools

    def _resolve_providers(
        self, agent: Agent[Any], resources: AsyncExitStack
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
        # from a string spec) vs caller-owned distinction below would be wrong;
        # the assert fails loudly if that invariant is ever broken.
        assert len(providers) == len(specs), (
            "resolve_providers() must return one provider per model spec"
        )
        for spec, provider in zip(specs, providers):
            if isinstance(spec, str):
                aclose = getattr(provider, "aclose", None)
                if callable(aclose):
                    _push_cleanup(resources, aclose)
        return providers

    async def _connect_workspace(
        self, agent: Agent[Any], resources: AsyncExitStack
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

    async def _persist_session(self, state: RunState) -> None:
        # Append this run's own entries as one segment. Prior history is already
        # in the Session and stays immutable; ``run_entries`` excludes the system
        # entry (it lives before ``run_start``).
        assert self.session is not None and self.session_id is not None
        run_entries = state.run_entries
        if run_entries:
            # Carry the policy's cross-run decisions in the segment ``meta`` so a
            # follow-up run resumes them without re-deriving — durably, unlike
            # the old in-process cache. The value is opaque to the loop.
            carryover_fn = getattr(self.context_policy, "carryover", None)
            carryover = (
                carryover_fn(state.context_policy_state)
                if callable(carryover_fn)
                else None
            )
            meta = {CARRYOVER_META_KEY: carryover} if carryover is not None else None
            # Key the segment by the run's ``run_id`` (passed as the ``run_id=``
            # argument; ``None`` when not checkpointing -> the store generates one).
            # Append is idempotent on it, so a resumed completion that the
            # crash-window left re-runnable can never double-write the run.
            await self.session.append(
                self.session_id, run_entries, run_id=self.run_id, meta=meta
            )

    async def _build_view(
        self, state: RunState, result: ContextResult
    ) -> list[TranscriptEntry]:
        """Return the per-call view to send to the provider.

        Compaction is view-only: ``state.transcript`` is never mutated. When the
        policy dropped the leading system message(s) (e.g. it summarized the
        head), re-prepend the system entries already stored at the head of the
        transcript so provider adapters still see them.
        """
        if not result.changed:
            return state.transcript
        view = result.entries
        if leading_system_count(view):
            return view
        # The compacted view dropped the leading system run. Re-prepend the
        # *existing* one(s) from the transcript rather than re-rendering: a fresh
        # render would re-run dynamic instruction fragments at a later
        # ``ctx.turn`` and could diverge from both the stored entries and what
        # ``RunContext.system_prompt`` reports — this keeps the view's system
        # text identical to what every other turn (and the property) sees.
        #
        # Restores the whole leading ``system`` run (the convention every model
        # call and the provider adapters use), NOT the runner-head count
        # (``system_head_len``) that handoff uses. The two answer different
        # questions: a handoff must NOT strip a caller-supplied leading
        # ``system`` input (it is run content), whereas here those same entries
        # ARE the system the model normally sees and must be restored. Slice
        # only the small head — never copy the (potentially large) body.
        n = leading_system_count(state.transcript)
        if n:
            return [*state.transcript[:n], *view]
        return view

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
