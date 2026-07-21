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
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

if TYPE_CHECKING:
    from ..messages import Message
    from ..workspace.protocol import WorkspaceSession

from .. import events
from .checkpoint import CheckpointWriter
from .resume import (
    normalize_replayed_entries,
    resolve_resume_agent,
    result_from_completed_snapshot,
)
from .model_turn import stream_model_turn
from .run_state import ActiveAgent, ModelTurnResult, PluginActivation, RunState
from .utils import (
    agent_model_label,
    input_preview,
    supports_json_schema,
    truncate_repr,
)
from .tool_calls import PreflightResult, ToolCallProcessor
from ..agent import Agent
from ..approvals import ApprovalChannel
from ..checkpointer import CheckpointOptions
from ..context import CompactionRequest, Compaction, ContextPolicy, ContextResult
from ..exceptions import (
    BudgetExceeded,
    ContextOverflowError,
    MaxTurnsExceeded,
    OutputValidationError,
    RunCancelled,
    UserError,
)
from ..guardrails import (
    check_input_guardrails,
    check_output_guardrails,
)
from ..handoff import Handoff, build_handoff_tool
from ..hooks import dispatch
from ..steering import Mailbox
from ..transcript import (
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
    TranscriptEntry,
    entries_to_messages,
    leading_system_count,
    messages_to_entries,
    to_json_safe,
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
from ..providers.base import (
    Provider,
    context_window,
    discover_context_window,
)
from ..reliability import CancelToken, RetryPolicy, RunBudget
from ..run_context import RunContext
from .result import RunResult
from ..session import STATE_META_KEY, NOTICE_META_KEY, Segment, Session
from ..tools import Tool
from ..tracing import NoopTracer, Span, Tracer, handoff_span, record_run_end, run_span

logger = logging.getLogger(__name__)

# Sentinel distinguishing "no final output yet" from a legitimate ``None``
# output (e.g. an Optional output_type).
_UNSET: object = object()

# Sentinel distinguishing a policy that declares ``context_window`` and left it
# unset (ask the endpoint) from one that has no such field at all (never needs
# a window — don't spend a request on it).
_NO_WINDOW_FIELD: object = object()

# After a context overflow, retry the model call only if reactive compaction
# shrank the estimated prompt below this fraction of the size that failed.
# Deliberately permissive — it exists to skip near-no-op retries, not to
# second-guess a real shrink.
_RETRY_SHRINK_FACTOR = 0.95


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
        mailbox: Mailbox | None = None,
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
        # the caller didn't pass one, a fresh token is never canceled, so this
        # is behaviourally identical to the old None for callers who don't reach
        # for it.
        self.cancel_token = cancel_token or CancelToken()
        # Inbound steering channel (the dual of cancel_token, given the same
        # treatment): always hold one so it can be exposed on RunContext for
        # tools and hooks to push into. A runner-created default has no outside
        # reference — anything still queued when the run ends is unreachable —
        # so the leftover hand-off to a next run only works for caller-supplied
        # mailboxes. Not ``or``: an *empty* Mailbox is falsy (``__bool__``) and
        # must not be swapped out for a fresh one.
        self.mailbox = mailbox if mailbox is not None else Mailbox()
        # Run-scoped observability. ``None`` → NoopTracer at stream() time, so
        # instrumentation stays free. A run-level knob (like budget/cancel_token),
        # not a per-agent one: it applies across handoffs to whatever agent is
        # active, which a field on the initial agent could not express.
        self.tracer = tracer
        self.retry = retry
        # Belt for direct RunLoop construction; the public path (Runner)
        # already resolved this to the agent's policy. Compaction() sizes
        # itself to the provider's advertised window at call time.
        self.context_policy: ContextPolicy = context_policy or Compaction()
        self.run_id = checkpoint.resolved_run_id if checkpoint is not None else None
        self.checkpointer = checkpoint.checkpointer if checkpoint is not None else None
        # Resolved lazily in ``_resolve_resume``: a snapshot passed in directly,
        # or one loaded by ``run_id`` per the ``if_run_exists`` policy.
        self.resume_from = checkpoint.resume_from if checkpoint is not None else None
        # The active agent to resume as, resolved from ``initial_agent``'s
        # handoff graph by ``_resolve_resume`` (the snapshot's agent may be a
        # handoff target, not the entry agent). ``None`` for a fresh run.
        self._resume_agent: Agent[Any] | None = None
        # The context policy's carried state from a *completed* snapshot,
        # stashed by ``_resolve_resume`` so the replay path can rebuild the
        # session-segment ``meta`` when it heals a missed session append.
        self._completed_context_state: dict[str, Any] | None = None
        self.if_run_exists = (
            checkpoint.if_run_exists if checkpoint is not None else "resume"
        )
        self.extra_instructions = extra_instructions
        # Activated agents, keyed by agent identity. A handoff that returns to
        # an agent seen earlier in the run reuses its bundle instead of
        # re-opening provider clients, workspace sessions, and plugin
        # connections — activation is once per agent per run (see
        # ``_resolve_active``); the agent objects live on the caller's handoff
        # graph for the whole run, so identity keys cannot be reused.
        self._activated: dict[int, ActiveAgent] = {}
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

    async def _drain_mailbox(self, state: RunState) -> AsyncIterator[events.Event]:
        """Append any mailbox-injected messages as ``user`` turns.

        Called at the start of each turn (a safe point): each queued item
        becomes an ``InputEntry`` the model sees on its next call, and a
        :class:`events.UserMessageInjected` lets a live consumer render it.
        Whatever is pushed after the last turn-start drain stays queued; a
        caller who supplied the mailbox can feed it into the next run.
        """
        for content in self.mailbox.drain():
            state.transcript.append(InputEntry(role="user", content=content))
            yield await self._emit(
                state, events.UserMessageInjected(content=content, turn=state.turns)
            )

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
                # completion; replay folds usage, re-applies session
                # persistence idempotently, and clears the checkpoint.
                if self.parent_usage is not None:
                    self.parent_usage.add(completed.usage)
                if self.session is not None:
                    # Heal the crash window between checkpoint completion and
                    # session append: ``Session.append`` is idempotent on
                    # ``run_id``, so when the original completion already
                    # persisted this is a no-op — and when it crashed first,
                    # the session would otherwise be missing this run forever.
                    # Before the checkpoint delete, mirroring the normal
                    # completion order (checkpoint finalized, then session).
                    await self._append_session_segment(
                        completed.entries,
                        context_state=self._completed_context_state or {},
                        notice=None,  # not persisted in snapshots
                    )
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
                    state.agent.input_guardrails + state.active.plugins.input_guardrails
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
                    # Fold in any messages injected since this turn's predecessor
                    # began, as ``user`` entries the model sees on this call.
                    drained_any = False
                    async for ev in self._drain_mailbox(state):
                        drained_any = True
                        yield ev
                    if drained_any:
                        # Persist immediately: the messages are already consumed
                        # from the mailbox, so until the post-model save they
                        # would exist nowhere durable — a crash during the model
                        # call would silently drop them.
                        await self.checkpoints.save_running(state)
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
                    # Remembered for RunResult.finish_reason: the last turn's
                    # value is the run's final one.
                    state.last_finish_reason = assistant.finish_reason
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
                    # The turn has contributed entries to the transcript: from
                    # here on an abort must not roll back the turn counter —
                    # ``save_terminal`` persists these entries under this turn
                    # (so even a failing ``save_running`` below keeps the
                    # snapshot's turn count aligned with its entries).
                    turn_durable = True
                    yield await self._emit(
                        state, events.MessageCompleted(entries=turn.turn_entries)
                    )
                    # Persist requested tool calls before executing them, so a
                    # crash mid-execution can resume by draining the calls
                    # that have no matching result yet.
                    await self.checkpoints.save_running(state)

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
                # A crash (or store error) between the two is healed on replay:
                # the completed-snapshot path above re-appends idempotently.
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
                    # Shield the terminal save: a run canceled via
                    # wait_for/timeout must still leave an ``interrupted``
                    # snapshot. Without the shield, awaiting here could itself
                    # be canceled and drop the checkpoint.
                    await asyncio.shield(self.checkpoints.save_terminal(state, exc))
                    # A failed sub-run's spend is still real spend: fold what
                    # accumulated up to the failure into the parent's books
                    # (``_finalize_run`` only does this on success), so an
                    # agent-as-tool sub-run that trips its own budget doesn't
                    # vanish from the parent's usage and budget enforcement.
                    if self.parent_usage is not None:
                        self.parent_usage.add(state.run_ctx.usage)
                if isinstance(exc, Exception):
                    yield await self._emit(state, events.RunFailed(error=exc))
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

        if snapshot.status == "completed":
            # Replay needs the recorded agent only for result attribution and
            # output-type coercion. If the handoff graph changed since the run
            # completed (agent renamed or removed), degrade to the entry agent
            # with a warning instead of failing a run whose work is done — a
            # worker re-issuing idempotent run_ids across a deploy must not
            # error on finished runs. Resumable snapshots below keep the hard
            # error: continuing *execution* as the wrong agent is dangerous.
            try:
                active_agent = resolve_resume_agent(self.initial_agent, snapshot)
            except UserError:
                logger.warning(
                    "run.replay: recorded agent %r is no longer reachable from "
                    "entry agent %r; attributing the replayed result to the "
                    "entry agent",
                    snapshot.agent_name,
                    self.initial_agent.name,
                )
                active_agent = self.initial_agent
            self._completed_context_state = dict(snapshot.context_state)
            return result_from_completed_snapshot(
                active_agent, snapshot, output_type=self.output_type_override
            )
        active_agent = resolve_resume_agent(self.initial_agent, snapshot)
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
            mailbox=self.mailbox,
            # The caller's tracer verbatim (None when untraced), carried as
            # internal plumbing so agent-as-tool sub-runs join this trace.
            _tracer=self.tracer,
        )
        # Read prior session history once, as segments: the flattened entries
        # become the prefix, and the latest segment's ``meta`` seeds a fresh
        # run's context-policy state (below).
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
            # Normalized at the load boundary: the checkpoint may hold a
            # rejected call's original non-wire-safe arguments (persisted
            # with its model turn, before the in-memory normalization ran).
            # Pending calls stay raw for the resume drain to re-reject.
            run_ctx.entries.extend(normalize_replayed_entries(snapshot.entries))
            run_ctx.usage.add(snapshot.usage)
            # These entries are already in the checkpoint; only persist new ones.
            self.checkpoints.resume_at(len(snapshot.entries))
        else:
            run_ctx.entries.extend(self._user_input_entries())

        # Seed the context policy's per-run scratch: the resuming run's own
        # checkpoint wins; otherwise a fresh run inherits the previous run's
        # carried decisions from the latest session segment's meta; else empty.
        if snapshot is not None:
            context_state = dict(snapshot.context_state)
        elif segments:
            prior = (segments[-1].meta or {}).get(STATE_META_KEY)
            context_state = dict(prior) if isinstance(prior, dict) else {}
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
            context_state=context_state,
        )

    async def _resolve_active(
        self, agent: Agent[Any], resources: AsyncExitStack
    ) -> ActiveAgent:
        """Resolve everything derived from ``agent`` into one swappable bundle.

        Called at bootstrap and on every handoff, but each agent is activated
        **once per run**: a handoff returning to an already-activated agent
        reuses its bundle. Without the memo, an A→B→A ping-pong would open a
        fresh provider client, workspace session, and plugin connections per
        transfer — all held until run end — and re-run plugin ``setup``,
        losing run-scoped plugin state each time. Provider, workspace, and
        plugin connections are run-scoped: opened here, torn down when the
        run ends (a handoff leaves the previous agent's connections open
        until then — closing them eagerly would add failure modes for no
        gain).
        """
        cached = self._activated.get(id(agent))
        if cached is not None:
            return cached
        provider = self._resolve_provider(agent, resources)
        await self._ensure_context_window(provider)
        structured_output = resolve_structured_output(
            self._resolve_output_type(agent),
            supports_json_schema(provider),
        )
        workspace, workspace_tools = await self._connect_workspace(agent, resources)
        plugins = await self._activate_plugins(agent, resources)
        tools_by_name = self._collect_tools(agent, workspace_tools, plugins.tools)
        active = ActiveAgent(
            agent=agent,
            provider=provider,
            structured_output=structured_output,
            tools_by_name=tools_by_name,
            workspace=workspace,
            plugins=plugins,
        )
        self._activated[id(agent)] = active
        return active

    async def _activate_plugins(
        self, agent: Agent[Any], resources: AsyncExitStack
    ) -> PluginActivation:
        """Activate ``agent.plugins`` for one run, collecting their contributions.

        ``setup`` is awaited once per plugin **per run** — the activation memo
        in ``_resolve_active`` holds even across repeated handoffs — so any
        run-scoped state (and async resources like MCP connections) is opened
        once and all of a plugin's contributions (tool, injector, ...) share
        it. Each instance's ``aclose``
        is registered for best-effort teardown when the run ends (LIFO).

        A plugin's ``name`` is its identity: it must be unique within an agent.
        Validated up front (before any ``setup``) so a duplicate is rejected
        without opening — then tearing down — a plugin's resources.
        """
        seen: set[str] = set()
        for plugin in agent.plugins:
            if plugin.name in seen:
                raise UserError(
                    f"Duplicate plugin name {plugin.name!r} on agent {agent.name!r}.",
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
        # Mirror the restored turn counter onto the public context, exactly as
        # a normal turn start does — tools draining here must see the turn
        # they belong to, not the RunContext default of 0.
        state.run_ctx.turn = state.turns
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
        provider = state.active.provider
        request = CompactionRequest(
            # A shallow snapshot, not the live list: the policy contract says
            # read-only, but handing out the real transcript would let one
            # misbehaving user policy corrupt run state that the Session and
            # checkpoint then persist. The entry objects are still shared —
            # only the list is defensive. The transcript cannot grow between
            # here and the overflow re-compact below (both happen before this
            # turn's entries are appended), so the snapshot stays current.
            entries=list(state.transcript),
            provider=provider,
            model=getattr(provider, "model", None),
            # The same tool set _call_model serializes into the request, so
            # the policy can count the schema payload it will actually pay for.
            tools=list(state.active.tools_by_name.values()),
            last_input_tokens=state.last_input_tokens,
            overflow=False,
            scratch=state.context_state,
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
            async for ev in self._call_model(state, provider, view, turn, tracer):
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
            # This turn's first compact() already calibrated against
            # ``last_input_tokens``, and that count describes the *previous*
            # turn's view — pairing it with the estimate of the larger view
            # that just overflowed would drag the calibration ratio down
            # exactly when the estimate must stay conservative.
            failed_tokens = ctx_result.tokens_after
            request.last_input_tokens = None
            request.overflow = True
            request.reported_window = overflow.reported_window
            ctx_result = await self.context_policy.compact(request)
            # Retry only when the rebuilt view is meaningfully smaller than
            # the one that just failed (both numbers come from the same
            # estimator, so the comparison is scale-free). A near-identical
            # prompt is doomed to the same 400 — surface the overflow instead
            # of paying for it. Any real shrink still gets its one retry:
            # vetoing it would turn a possible recovery into a certain death.
            shrunk = (
                ctx_result.tokens_after is None
                or failed_tokens is None
                or ctx_result.tokens_after < failed_tokens * _RETRY_SHRINK_FACTOR
            )
            if not ctx_result.compacted or not shrunk:
                logger.error(
                    "context.overflow: policy could not shrink the prompt "
                    "(est. %s -> %s tokens); surfacing ContextOverflowError",
                    failed_tokens,
                    ctx_result.tokens_after,
                )
                raise
            view = await self._build_view(state, ctx_result)

        yield await self._emit(
            state, self._compacted_event(state, view, ctx_result, reactive=True)
        )
        view = await self._augment_view(state, view)
        turn.assistant = None
        turn.turn_entries = []
        async for ev in self._call_model(state, provider, view, turn, tracer):
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
        provider: Provider,
        view: list[TranscriptEntry],
        turn: ModelTurnResult,
        tracer: Tracer,
    ) -> AsyncIterator[events.Event]:
        async for ev in stream_model_turn(
            agent=state.agent,
            provider=provider,
            input_entries=view,
            tools_by_name=state.active.tools_by_name,
            structured_output=state.active.structured_output,
            tracer=tracer,
            turn=state.turns,
            result=turn,
            retry=self.retry,
            cancel_token=self.cancel_token,
        ):
            yield await self._emit(state, ev)

    async def _tool_phase(
        self,
        state: RunState,
        processor: ToolCallProcessor,
        calls: list[ToolCall],
        tracer: Tracer,
    ) -> AsyncIterator[events.Event]:
        """Execute one turn's tool calls, concurrently where allowed.

        Two-phase per call: ``preflight`` (cancel/budget, lookup, handoff
        dedup, argument parsing, approval) runs serially in request order on
        this generator body — so approval backpressure and budget determinism
        are exactly the serial loop's — while ready calls with
        ``Tool.parallel=True`` execute in background tasks whose events
        funnel through one queue back onto this body. That drain point (see
        :meth:`_drain_batch`) is the single place events reach hooks and the
        consumer and checkpoints are saved, preserving both the hook-ordering
        contract and the per-result durability cadence. Transcript results
        therefore append in completion order — safe, everything downstream is
        ``call_id``-keyed (providers, resume drain, compaction windows).

        ``Tool.parallel=False`` — and every handoff tool, whatever its flag —
        is an execution barrier: in-flight calls finish first, the tool runs
        inline and alone, then spawning resumes. A batch of only such tools
        reproduces the serial loop verbatim.

        Aborts: a ``BudgetExceeded`` from preflight stops *spawning* but lets
        in-flight calls finish and persist (the RunBudget contract) — only
        unstarted calls are left dangling for a resume to drain. Anything
        else (``RunCancelled``, a checkpoint store failure, an unexpected
        bug, the consumer abandoning the stream) cancels the in-flight tasks
        promptly; their calls dangle and a resume re-executes them, exactly
        as a serial abort left every not-yet-run call.
        """
        batch = _ToolBatch()
        budget_abort: BudgetExceeded | None = None
        try:
            for call in calls:
                # Surface (and persist) finished work before the next gate —
                # latency only; correctness never depends on this sweep.
                async for ev in self._drain_batch(state, batch, wait=False):
                    yield ev

                outcome = PreflightResult()
                try:
                    async for ev in processor.preflight(call, outcome, state=state):
                        yield await self._emit(state, ev)
                        if isinstance(ev, events.ToolCallCompleted):
                            # Preflight rejections append their error entry
                            # too — keep the per-result save cadence for them.
                            await self.checkpoints.save_running(state)
                except BudgetExceeded as exc:
                    budget_abort = exc
                    break
                if outcome.ready is None:
                    continue  # rejected: entry + ToolCallCompleted already out
                tool, args = outcome.ready

                if tool._handoff or not tool.parallel:
                    # Execution barrier. ``_handoff`` is checked on its own so
                    # a hand-built Tool(_handoff=True, parallel=True) still
                    # cannot race the first-handoff-wins dedup.
                    async for ev in self._drain_batch(state, batch, wait=True):
                        yield ev
                    async for ev in processor.execute(
                        call, tool, args, state=state, tracer=tracer
                    ):
                        yield await self._emit(state, ev)
                    # Persist after each tool result so a crash mid
                    # tool-execution can resume by draining the calls that
                    # still have no matching result.
                    await self.checkpoints.save_running(state)
                else:
                    gen = processor.execute(
                        call, tool, args, state=state, tracer=tracer
                    )
                    batch.active += 1
                    # create_task copies the current context, so the
                    # ContextVar-based tracer depth nests per task.
                    batch.tasks.append(asyncio.create_task(_pump(gen, batch.queue)))

            async for ev in self._drain_batch(state, batch, wait=True):
                yield ev
            if budget_abort is not None:
                # Raised only after the drain: in-flight results are saved,
                # so a resume re-executes nothing that already ran.
                raise budget_abort
        finally:
            # Reached on: a pump failure re-raised by a drain, RunCancelled or
            # BudgetExceeded from preflight, a save_running store failure,
            # GeneratorExit (consumer abandoned the stream), or normal
            # completion (everything below is then a no-op). NO yields here —
            # an async generator must not yield while closing; plain awaits
            # are fine. If the consuming task is itself being canceled the
            # gather may be interrupted after cancel() was already delivered —
            # the tasks then die on their own (best-effort teardown, matching
            # the shield-only-the-terminal-save posture of the run loop).
            for task in batch.tasks:
                task.cancel()
            if batch.tasks:
                await asyncio.gather(*batch.tasks, return_exceptions=True)
                # Pumps swallow their exception into the queue marker (the
                # tasks themselves never carry one), so secondary failures —
                # siblings that broke while the first failure was already
                # propagating — are logged from the abandoned queue.
                while True:
                    try:
                        item = batch.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if (
                        isinstance(item, _PumpEnded)
                        and item.error is not None
                        and not isinstance(
                            item.error, (asyncio.CancelledError, RunCancelled)
                        )
                    ):
                        logger.warning(
                            "tool.batch_teardown: sibling tool call also failed: %r",
                            item.error,
                        )

    async def _drain_batch(
        self, state: RunState, batch: _ToolBatch, *, wait: bool
    ) -> AsyncIterator[events.Event]:
        """Surface queued events from the batch's in-flight tool tasks.

        ``wait=False``: pop whatever is ready and return (the between-calls
        sweep). ``wait=True``: block until every spawned pump has ended (the
        barrier / end-of-batch drain) — when ``batch.active`` hits zero the
        queue is exhausted, since nothing enqueues after the last marker.

        This is the batch's single serialization point: every event reaches
        hooks and the consumer here (``_emit`` + yield, in queue order), and
        each ``ToolCallCompleted`` is followed by a checkpoint save —
        ``CheckpointWriter`` bookkeeping assumes saves never overlap, so they
        must only ever happen on this generator body, never inside tasks. A
        failure marker re-raises the pump's exception (first failure wins);
        teardown of the surviving tasks belongs to ``_tool_phase``'s
        ``finally``, so abandoning this generator mid-drain is safe.
        """
        while batch.active > 0 or not batch.queue.empty():
            if wait:
                item = await batch.queue.get()
            else:
                try:
                    item = batch.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
            if isinstance(item, _PumpEnded):
                batch.active -= 1
                if item.error is not None:
                    raise item.error
                continue
            yield await self._emit(state, item)
            if isinstance(item, events.ToolCallCompleted):
                # Persist after each tool result so a crash mid tool-execution
                # can resume by draining the calls that still have no matching
                # result.
                await self.checkpoints.save_running(state)
                # Serial parity for cooperative cancellation: the serial loop
                # checked the token before each call; check after each
                # completed result so a mid-batch cancel() stops the batch at
                # the next result instead of the next turn.
                self.cancel_token.check()

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
        if not assistant.content and state.active.structured_output is None:
            # No content and no tool calls still completes the run (the empty
            # string is the output), but it is almost always a provider hiccup
            # or a max_tokens truncation — leave a trace. The structured-output
            # path needs no twin: an empty reply fails to parse there and goes
            # through output repair instead.
            logger.warning(
                "run.empty_output: model returned no content and no tool calls "
                "(finish=%s); run completes with empty output",
                assistant.finish_reason,
            )
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
            # This run's own entries (its input + what it produced), NOT the full
            # transcript — consistent with the resume-from-snapshot path, which
            # rebuilds exactly these. The full transcript (system + prior history
            # + this run) is ``RunContext.entries`` / ``Session.load()``.
            entries=state.run_entries,
            final_agent=state.agent,
            usage=state.run_ctx.usage,
            turns=state.turns,
            finish_reason=state.last_finish_reason,
            last_input_tokens=state.last_input_tokens,
        )

        if self.parent_usage is not None:
            self.parent_usage.add(state.run_ctx.usage)

        record_run_end(
            span, turns=state.turns, total_tokens=state.run_ctx.usage.total_tokens
        )
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
        prefix and the carried-state meta). Everything appended after this prefix
        is the run's own contribution; its length is the run's
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

    async def _ensure_context_window(self, provider: Provider) -> None:
        """Give the endpoint a chance to report its own context window.

        The adapter decides whether that costs anything: it declines when the
        endpoint is known to publish none and when it already asked (memoized
        per endpoint, for the process). What we must *not* do here is skip the
        question because the bundled table has an answer — the endpoint
        outranks the table, and a deployment that caps a familiar model would
        otherwise be budgeted at the table's number.

        The sentinel matters: a policy that does not *declare* ``context_window``
        (:class:`~lovia.context.NoopContextPolicy`, custom policies) never needs
        a window, and must not pay for one.
        """
        declared = getattr(self.context_policy, "context_window", _NO_WINDOW_FIELD)
        if declared is not None:
            return
        await discover_context_window(provider)
        if context_window(provider) is None:
            logger.info(
                "context.window: unknown for %r; proactive compaction is off — "
                "set Compaction(context_window=...) if the endpoint cannot report it",
                getattr(provider, "model", None),
            )

    def _resolve_provider(
        self, agent: Agent[Any], resources: AsyncExitStack
    ) -> Provider:
        """Resolve ``agent``'s provider once for the rest of the run.

        A provider built here from a string spec is owned by the run: its
        lazily-created HTTP client is reused across turns and closed when
        the run ends. A user-supplied :class:`Provider` instance is never
        closed — its lifecycle belongs to the caller.
        """
        provider = agent.resolve_provider()
        if isinstance(agent.model, str):
            aclose = getattr(provider, "aclose", None)
            if callable(aclose):
                _push_cleanup(resources, aclose)
        return provider

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
        await self._append_session_segment(
            state.run_entries,
            context_state=state.context_state,
            notice=state.context_notice,
        )

    async def _append_session_segment(
        self,
        entries: list[TranscriptEntry],
        *,
        context_state: dict[str, Any],
        notice: events.CompactionNotice | None,
    ) -> None:
        """Append one run's ``entries`` to the Session as a segment.

        Shared by normal completion (:meth:`_persist_session`) and the
        completed-snapshot replay path, which re-applies persistence to heal
        the crash window between checkpoint finalization and session append.

        ``meta`` hosts two independent co-tenants (either may be absent):

        * the policy's carried state — the same opaque scratch the checkpoint
          stores — so the next run on this session resumes its decisions
          without re-deriving them;
        * the run's last compaction notice, for the web UI to replay on
          reload (the replay path passes ``None``: not persisted in snapshots).
        """
        assert self.session is not None and self.session_id is not None
        if not entries:
            return
        meta: dict[str, Any] = {}
        if context_state:
            # Sanitize exactly as the checkpoint path does (which stores
            # ``to_json_safe(context_state)``) — same blob, same treatment —
            # so a custom policy's scratch can't round-trip through the
            # checkpoint yet crash the session store here.
            meta[STATE_META_KEY] = to_json_safe(context_state)
        if notice is not None:
            meta[NOTICE_META_KEY] = asdict(notice)
        # Key the segment by the run's ``run_id`` (passed as the ``run_id=``
        # argument; ``None`` when not checkpointing -> the store generates
        # one). Append is idempotent on it, so a completed run's entries land
        # in the session exactly once no matter how often it is replayed.
        await self.session.append(
            self.session_id, entries, run_id=self.run_id, meta=meta or None
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
        # Build the notice once and reuse it: the live event carries it, and it is
        # remembered on ``state.context_notice`` so the finished segment can stow
        # it for the web UI to replay on reload. The last compaction of the run
        # wins (its cumulative counts are the most complete).
        notice = events.CompactionNotice(
            reason=result.reason or "context_policy",
            reactive=reactive,
            summary=result.summary,
            tokens_before=result.tokens_before,
            tokens_after=result.tokens_after,
            detail=result.detail,
        )
        state.context_notice = notice
        return events.ContextCompacted(
            session_id=self.session_id,
            entries_before=list(state.transcript),
            entries_after=list(view),
            notice=notice,
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


@dataclass
class _PumpEnded:
    """Terminal marker a pump enqueues after its execute() stream ends.

    ``error`` carries the exception that ended the stream (``RunCancelled``
    escaping a tool, an unexpected bug, or the ``CancelledError`` injected by
    teardown) instead of leaving it on the task: the drain loop re-raises the
    first failure it dequeues, and teardown never sees unretrieved task
    exceptions.
    """

    error: BaseException | None = None


@dataclass
class _ToolBatch:
    """Drain state for one turn's tool batch (single consumer: the loop)."""

    queue: "asyncio.Queue[events.Event | _PumpEnded]" = field(
        default_factory=asyncio.Queue
    )
    tasks: list["asyncio.Task[None]"] = field(default_factory=list)
    # Pumps spawned minus _PumpEnded markers dequeued. Zero means every
    # spawned execute() stream has ended AND its events are already queued
    # (FIFO: a task's events always precede its own marker).
    active: int = 0


async def _pump(
    gen: AsyncIterator[events.Event],
    queue: "asyncio.Queue[events.Event | _PumpEnded]",
) -> None:
    """Drive one execute() generator, forwarding its events into ``queue``.

    ALWAYS ends by enqueueing :class:`_PumpEnded` — the drain loop counts
    markers to know when the batch is done. Exceptions ride the marker rather
    than staying on the task (see :class:`_PumpEnded`). ``put_nowait`` on the
    unbounded queue is synchronous, so this coroutine's only suspension
    points are inside the generator (i.e. inside the tool) — a teardown
    ``cancel()`` can only land there, where execute()'s exception structure
    already leaves no bogus result entry behind.
    """
    error: BaseException | None = None
    try:
        async for ev in gen:
            queue.put_nowait(ev)
    except BaseException as exc:  # noqa: BLE001 — forwarded via the marker
        error = exc
    queue.put_nowait(_PumpEnded(error=error))


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
