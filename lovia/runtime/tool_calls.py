"""Tool-call execution and approval handling for the runner."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .. import events
from ..approvals import ApprovalChannel
from ..handoff import _HandoffSignal
from ..messages import ToolCall
from ..exceptions import RunCancelled
from ..reliability import CancelToken, RunBudget
from .run_state import RunState
from .utils import truncate_repr
from ..tools import render_tool_result, run_tool, truncate_tool_output
from ..tracing import Tracer, tool_call_span
from ..transcript import ToolResultEntry

logger = logging.getLogger(__name__)

# Rendered-output size above which the tool's raw return value is not
# retained on the transcript entry, even when nothing is truncated. ``raw``
# mirrors the output (for a structured result it is the object tree behind
# the JSON dump), so keeping it roughly doubles what run memory and every
# checkpoint/session serialization pay for a huge entry — for a field whose
# only job is post-hoc inspection. Hooks observe the original result on the
# transient ``ToolCallCompleted`` event regardless.
_KEEP_RAW_MAX_CHARS = 16_000


@dataclass
class ToolCallProcessor:
    approvals: ApprovalChannel
    cancel_token: CancelToken = field(default_factory=CancelToken)
    budget: RunBudget | None = None

    async def process(
        self,
        call: ToolCall,
        *,
        state: RunState,
        tracer: Tracer,
    ) -> AsyncIterator[events.Event]:
        """Execute one assistant-requested tool call.

        Appends a :class:`ToolResultEntry` to ``state.transcript`` in every
        outcome (missing tool, duplicate handoff, malformed arguments, denial,
        error, success) so the transcript never contains a dangling tool call.
        A handoff tool records its signal in ``state.pending_handoff``; the
        first handoff of a turn wins and later ones are rejected unrun.
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
        # ``ToolCallCompleted(is_error=True)`` — not as ``ErrorOccurred``, which
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
            # transcript never claims a transfer that won't happen.
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
            # running the tool with empty arguments.
            err = f"Invalid JSON in tool arguments: {exc}"
            logger.warning(
                "tool.bad_arguments: %s call_id=%s args=%s",
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
            yield events.ErrorOccurred(error=exc)
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
                    yield events.ErrorOccurred(error=exc)
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
            # Consistent with the pre-tool ``cancel_token.check()`` above, and the
            # path an agent-as-tool sub-run takes when it inherits and trips the
            # parent's token.
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
            yield events.ErrorOccurred(error=exc)

        if isinstance(result, _HandoffSignal):
            state.pending_handoff = result
            result_text = f"Transferred to {result.handoff.target.name}" + (
                f" ({result.reason})" if result.reason else ""
            )
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
                # re-execute the (possibly non-idempotent) tool.
                logger.warning(
                    "tool.render_error: %s call_id=%s (%s: %s)",
                    tool.name,
                    call.id,
                    type(exc).__name__,
                    exc,
                )
                yield events.ErrorOccurred(error=exc)
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
        — so the transcript and event shape can't drift between outcomes.
        """
        state.transcript.append(
            ToolResultEntry(call_id=call.id, output=err, is_error=True)
        )
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
