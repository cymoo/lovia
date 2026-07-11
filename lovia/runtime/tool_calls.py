"""Tool-call execution and approval handling for the runner.

One requested call flows through two stages, split so the loop can overlap
the slow part across calls:

* :meth:`ToolCallProcessor.preflight` — the serial gates (cancel/budget,
  tool lookup, handoff dedup, argument parsing, the approval flow). Runs on
  the loop's generator body, one call at a time, in request order.
* :meth:`ToolCallProcessor.execute` — the invocation itself (span, retries,
  rendering, truncation, the transcript append). May run concurrently with
  other calls of the same turn; see :meth:`RunLoop._tool_phase` for the
  orchestration and barrier semantics.

Between the two, every call that reaches a terminal outcome (missing tool,
duplicate handoff, malformed arguments, denial, error, success) appends
exactly one :class:`ToolResultEntry` — such calls never dangle. Calls
interrupted by an abort are the deliberate exception: ``RunCancelled``, a
checkpoint store failure, or the consumer abandoning the stream cancels the
turn's in-flight tasks, and a cancelled call leaves no entry — it stays
pending and a resume re-executes it (see ``pending_tool_calls``). Results
append in completion order — safe, because everything downstream pairs
calls to results by ``call_id``, never by position.
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, AsyncIterator

from .. import events
from ..approvals import ApprovalChannel
from ..handoff import _HandoffSignal
from ..messages import ToolCall
from ..exceptions import RunCancelled
from ..reliability import CancelToken, RunBudget
from .run_state import RunState
from .utils import truncate_repr
from ..tools import Tool, render_tool_result, run_tool, truncate_tool_output
from ..tracing import Tracer, tool_call_span
from ..transcript import ToolCallEntry, ToolResultEntry

logger = logging.getLogger(__name__)


def _normalize_call_args(state: RunState, call_id: str) -> None:
    """Rewrite a rejected call's transcript entry to wire-safe arguments.

    A model can emit malformed ``arguments`` — most often a stream truncated
    mid-call. Left raw in the transcript and echoed back next turn, they make
    the request invalid JSON and 400 the provider on every retry (it is the
    same bytes). Detection is the one place we already know the payload is
    bad, so we normalize the stored entry here — wrapping the offending text
    under ``_raw`` (a JSON object, original preserved) — and every serializer
    can then trust the transcript instead of re-validating it on each re-send.

    Replaces the entry rather than mutating it: the copy already handed to
    observers on ``MessageCompleted`` must keep the exact payload the model
    emitted.
    """
    for i in range(len(state.transcript) - 1, -1, -1):
        entry = state.transcript[i]
        if isinstance(entry, ToolCallEntry) and entry.call_id == call_id:
            try:
                parsed = json.loads(entry.arguments or "{}")
                # Wire-safe means a JSON *object*, not merely valid JSON: the
                # Anthropic adapter unpacks ``arguments`` into ``tool_use.input``,
                # which must be an object, so a bare array/scalar the model
                # should never emit for tool args (``"[1,2]"``, ``"5"``, ``"null"``)
                # still 400s it and must be wrapped too.
                wire_safe = isinstance(parsed, dict)
            except json.JSONDecodeError:
                wire_safe = False
            if not wire_safe:
                state.transcript[i] = replace(
                    entry, arguments=json.dumps({"_raw": entry.arguments})
                )
            return


# Rendered-output size above which the tool's raw return value is not
# retained on the transcript entry, even when nothing is truncated. ``raw``
# mirrors the output (for a structured result it is the object tree behind
# the JSON dump), so keeping it roughly doubles what run memory and every
# checkpoint/session serialization pay for a huge entry — for a field whose
# only job is post-hoc inspection. Hooks observe the original result on the
# transient ``ToolCallCompleted`` event regardless.
_KEEP_RAW_MAX_CHARS = 16_000


@dataclass
class PreflightResult:
    """Outcome accumulator for :meth:`ToolCallProcessor.preflight`.

    An async generator cannot return a value (same pattern as
    :class:`~lovia.runtime.run_state.ModelTurnResult`): preflight yields its
    events and records the decision here. ``ready`` is ``None`` when the call
    was rejected during preflight — its error :class:`ToolResultEntry` and
    ``ToolCallCompleted(is_error=True)`` were already produced — otherwise
    the resolved ``(tool, parsed_args)`` to execute.
    """

    ready: tuple[Tool, dict[str, Any]] | None = None


@dataclass
class ToolCallProcessor:
    approvals: ApprovalChannel
    cancel_token: CancelToken = field(default_factory=CancelToken)
    budget: RunBudget | None = None

    async def preflight(
        self,
        call: ToolCall,
        outcome: PreflightResult,
        *,
        state: RunState,
    ) -> AsyncIterator[events.Event]:
        """Gate one requested call before execution.

        Serial, in request order: cancel/budget checks, tool lookup, handoff
        dedup, argument parsing, and the approval flow. Every rejection
        appends its error :class:`ToolResultEntry` (via :meth:`_rejected`)
        and yields the matching ``ToolCallCompleted(is_error=True)``; only a
        call that passes every gate fills ``outcome`` for execution. A
        handoff already pending for this turn rejects later handoffs unrun —
        the first handoff of a turn wins, and the loser's ``on_handoff``
        side effects never fire. May raise :class:`RunCancelled` (cancel
        check) or :class:`BudgetExceeded` (per-call cap) — the only
        exceptions that escape rather than becoming rejections.
        """

        self.cancel_token.check()
        if self.budget is not None:
            # Every requested call counts toward max_tool_calls, including ones
            # rejected just below (unknown tool, malformed args, denied) — so a
            # model stuck spamming a bad tool name still hits the cap.
            self.budget.record_tool_call()
            # The only place the per-call cap is enforced mid-turn (turn-start
            # and post-model checks cover token caps, not the tool-call count).
            self.budget.check(state.run_ctx.usage)

        # Unknown tool and the malformed-arguments case below are *model-input*
        # errors, not runtime failures: they are fed back to the model as an
        # error tool-result and surfaced to observers via
        # ``ToolCallCompleted(is_error=True)`` — not as ``ToolCallFailed``, which
        # carries a ``BaseException`` (there is none here). No ``ToolCallStarted``
        # precedes them because the tool never starts; see the event docstrings.
        tool = state.active.tools_by_name.get(call.name)
        if tool is None:
            err = f"Tool {call.name!r} is not available."
            logger.warning("tool.unknown: %s call_id=%s", call.name, call.id)
            yield self._rejected(state, call, err)
            return

        if tool._handoff and state.pending_handoff is not None:
            # First handoff of the turn wins. Rejecting *before* invoke means
            # the loser's ``on_handoff`` side effects never fire and the
            # transcript never claims a transfer that won't happen. Sound
            # under parallel execution because handoff tools only ever run as
            # execution barriers: a winning handoff has fully executed (and
            # set ``pending_handoff``) before any later call is preflighted.
            target = state.pending_handoff.handoff.target.name
            err = (
                f"Handoff not performed: this turn already transferred to "
                f"{target!r}. Only the first handoff in a turn takes effect."
            )
            logger.warning(
                "tool.handoff_ignored: %s call_id=%s (already pending -> %r)",
                call.name,
                call.id,
                target,
            )
            yield self._rejected(state, call, err)
            return

        try:
            args: dict[str, Any] = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            # Feed the parse error back to the model instead of silently
            # running the tool with empty arguments. When the turn hit the
            # output-token ceiling, say so: the generic "invalid JSON" would
            # send the model looping on the same oversized call, never learning
            # it was truncated rather than malformed.
            if state.last_finish_reason == "length":
                err = (
                    f"Tool {call.name} arguments are incomplete: the response was "
                    "cut off at the output token limit (finish_reason=length) "
                    "mid-call. Emit a smaller call — e.g. write the file in "
                    "chunks / use append mode."
                )
            else:
                err = f"Invalid JSON in tool arguments: {exc}"
            logger.warning(
                "tool.bad_arguments: %s call_id=%s args=%s",
                call.name,
                call.id,
                truncate_repr(call.arguments),
            )
            yield self._rejected(state, call, err)
            return
        if not isinstance(args, dict):
            # Valid JSON but not an object ("[1,2]", "5"): it cannot bind to
            # tool parameters, and — worse — a stored entry replayed verbatim
            # 400s providers whose wire unpacks arguments into an object
            # (Anthropic's ``tool_use.input``), poisoning every later turn.
            # Rejecting here routes through ``_rejected``'s normalization,
            # which wraps the stored entry wire-safe like the unparseable case.
            err = (
                f"Invalid tool arguments: expected a JSON object, "
                f"got {type(args).__name__}."
            )
            logger.warning(
                "tool.bad_arguments: %s call_id=%s non-object args=%s",
                call.name,
                call.id,
                truncate_repr(call.arguments),
            )
            yield self._rejected(state, call, err)
            return

        try:
            needs_approval = tool.requires_approval(args, state.run_ctx)
        except Exception as exc:
            # Fail closed, and — like every other outcome — leave a
            # ToolResultEntry: a raising predicate must neither let the tool
            # run unvetted nor crash the run with a dangling call that a
            # resume would then re-execute.
            logger.warning(
                "tool.approval_predicate_error: %s call_id=%s (%s: %s)",
                tool.name,
                call.id,
                type(exc).__name__,
                exc,
            )
            yield events.ToolCallFailed(error=exc, call=call)
            err = f"Tool {call.name} was not approved (approval check failed)."
            yield self._rejected(state, call, err)
            return
        if needs_approval:
            # Fail-closed approval. Resolution order: the streaming consumer
            # (while suspended at the ApprovalRequired yield, or out-of-band
            # via the channel), then ``agent.approval_handler``, then deny.
            fut = self.approvals.register(call.id)
            yield events.ApprovalRequired(call=call, _channel=self.approvals)
            handler = state.agent.approval_handler
            if handler is not None and not fut.done():
                try:
                    decision = handler(call, state.run_ctx)
                    if inspect.isawaitable(decision):
                        decision = await decision
                except Exception as exc:
                    # ``Runner.run`` callers never see events, so the log is the
                    # only durable signal that the handler failed (and the call
                    # was therefore denied below).
                    logger.warning(
                        "approval_handler.error: call_id=%s (%s: %s)",
                        call.id,
                        type(exc).__name__,
                        exc,
                    )
                    yield events.ToolCallFailed(error=exc, call=call)
                    decision = False
                self._apply_handler_decision(call.id, decision)
            if not fut.done():
                # Nobody decided — deny so the run cannot hang.
                self.approvals.reject(call.id)
            approved = fut.result()
            self.approvals.discard(call.id)
            if not approved:
                denial = f"Tool {call.name} was not approved."
                yield self._rejected(state, call, denial)
                return

        outcome.ready = (tool, args)

    async def execute(
        self,
        call: ToolCall,
        tool: Tool,
        args: dict[str, Any],
        *,
        state: RunState,
        tracer: Tracer,
    ) -> AsyncIterator[events.Event]:
        """Run one preflighted call to its terminal outcome.

        ``ToolCallStarted``, the invocation inside a ``tool_call_span``,
        handoff-signal capture, rendering, truncation, the transcript append,
        and ``ToolCallCompleted``. Every terminal outcome (success, tool
        error, render failure) appends exactly one :class:`ToolResultEntry`;
        only a call whose task is cancelled mid-flight (sibling cancellation
        when the batch aborts) appends nothing — it stays a pending call that
        a resume re-executes. Re-raises :class:`RunCancelled` (a run-global
        signal); converts every other failure into an error result.

        May run concurrently with other ``execute`` calls of the same turn:
        it only appends to ``state.transcript`` (order-insensitive — results
        are ``call_id``-keyed) and, for handoff tools, writes
        ``state.pending_handoff`` (never racy: the loop runs handoffs as
        execution barriers, alone).
        """

        yield events.ToolCallStarted(call=call)
        logger.info(
            "tool.start: %s call_id=%s args=%s",
            tool.name,
            call.id,
            truncate_repr(args),
        )

        try:
            with tool_call_span(tracer, name=tool.name, call_id=call.id):
                result = await run_tool(
                    tool,
                    args,
                    state.run_ctx,
                    default_retries=state.agent.default_tool_retries,
                    default_timeout=state.agent.default_tool_timeout,
                )
            is_error = False
        except RunCancelled:
            # A run-control signal, not a tool failure — let it propagate so the
            # run terminates instead of being swallowed into a tool-error result.
            # Consistent with the pre-tool ``cancel_token.check()`` in preflight,
            # and the path an agent-as-tool sub-run takes when it inherits and
            # trips the parent's token.
            #
            # BudgetExceeded is deliberately NOT re-raised here: budgets are
            # scoped, not run-global. One escaping a tool is a *sub-run's own*
            # budget (agent-as-tool) — a recoverable delegation failure, fed
            # back to the model exactly like the sub-run's MaxTurnsExceeded.
            # This run's own budget needs no help from this path: the loop
            # re-checks it before the next tool call (in preflight) and at
            # every turn boundary, so an actually-exhausted run still stops at
            # the next safe point before any further model call or tool
            # execution.
            raise
        except Exception as exc:
            result = f"Tool error: {exc}"
            is_error = True
            logger.warning(
                "tool.error: %s call_id=%s (%s: %s)",
                tool.name,
                call.id,
                type(exc).__name__,
                exc,
            )
            yield events.ToolCallFailed(error=exc, call=call)

        if isinstance(result, _HandoffSignal):
            state.pending_handoff = result
            result_text = f"Transferred to {result.handoff.target.name}" + (
                f" ({result.reason})" if result.reason else ""
            )
        elif is_error:
            # The "Tool error: ..." string built above is runner-produced and
            # final. Renderers format a tool's *return value*; routing error
            # strings through them forced every renderer to special-case
            # strings, and a success-shape renderer would crash here and mask
            # the real failure behind "result rendering failed".
            result_text = result
        else:
            try:
                result_text = await render_tool_result(
                    tool,
                    result,
                    state.run_ctx,
                    default=state.agent.tool_result_renderer,
                )
            except RunCancelled:
                raise
            except Exception as exc:
                # The tool itself already ran; only rendering failed. Convert
                # to an error result instead of crashing the run — a crash
                # here would leave a dangling call, and a resume would then
                # re-execute the (possibly non-idempotent) tool. BudgetExceeded
                # included, as in the invoke path above: budgets are scoped,
                # and this run's own budget is re-checked at the next safe
                # point regardless.
                logger.warning(
                    "tool.render_error: %s call_id=%s (%s: %s)",
                    tool.name,
                    call.id,
                    type(exc).__name__,
                    exc,
                )
                yield events.ToolCallFailed(error=exc, call=call)
                result = f"Tool error: result rendering failed: {exc}"
                result_text = result
                is_error = True

        # Cap what enters the transcript: everything downstream (run memory,
        # checkpoints, session storage) pays for the full string, so huge
        # outputs are cut at the source and their retained raw value dropped.
        # The transient ToolCallCompleted event still carries the original
        # result for observability.
        raw_value: object = result
        limit = tool.max_output_chars
        if limit is None:
            limit = state.agent.max_tool_output_chars
        if limit is not None and len(result_text) > limit:
            logger.info(
                "tool.truncate: %s call_id=%s output %d chars > limit %d",
                tool.name,
                call.id,
                len(result_text),
                limit,
            )
            result_text = truncate_tool_output(result_text, limit)
            raw_value = None
        elif len(result_text) > _KEEP_RAW_MAX_CHARS:
            raw_value = None

        state.transcript.append(
            ToolResultEntry(
                call_id=call.id,
                output=result_text,
                raw=raw_value,
                is_error=is_error,
            )
        )
        if not is_error:
            logger.info(
                "tool.done: %s call_id=%s result=%s",
                tool.name,
                call.id,
                truncate_repr(result_text),
            )
        yield events.ToolCallCompleted(
            call=call, result=result, is_error=is_error, output=result_text
        )

    def _rejected(
        self, state: RunState, call: ToolCall, err: str
    ) -> events.ToolCallCompleted:
        """Record a pre-execution rejection and return the event to yield.

        The one place every rejected call (unknown tool, duplicate handoff,
        malformed arguments, failed approval check, denial) writes its error
        :class:`ToolResultEntry` and builds the matching ``ToolCallCompleted``
        — so the transcript and event shape can't drift between outcomes. Also
        the one place a malformed call is normalized for re-serialization: any
        rejection reason can pair with unparseable arguments, so we sanitize
        here rather than in only the JSON-decode branch.
        """
        state.transcript.append(
            ToolResultEntry(call_id=call.id, output=err, is_error=True)
        )
        _normalize_call_args(state, call.id)
        return events.ToolCallCompleted(
            call=call, result=err, is_error=True, output=err
        )

    def _apply_handler_decision(self, call_id: str, decision: object) -> None:
        # String decisions follow the declared ``ApprovalDecision`` contract
        # (``"allow"`` / ``"deny"`` / ``"ask"``). ``"deny"`` and anything
        # unrecognised resolve to deny, matching the run's fail-closed posture.
        if isinstance(decision, str):
            token = decision.strip().lower()
            if token == "ask":
                return  # defer; falls through to the default-deny check
            self.approvals.resolve(call_id, token == "allow")
        else:
            self.approvals.resolve(call_id, bool(decision))
