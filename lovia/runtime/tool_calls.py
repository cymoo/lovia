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
        outcome (missing tool, malformed arguments, denial, error, success)
        so the transcript never contains a dangling tool call. A handoff tool
        records its signal in ``state.pending_handoff``.
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
            state.transcript.append(
                ToolResultEntry(call_id=call.id, output=err, is_error=True)
            )
            yield events.ToolCallCompleted(
                call=call, result=err, is_error=True, output=err
            )
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
            state.transcript.append(
                ToolResultEntry(call_id=call.id, output=err, is_error=True)
            )
            yield events.ToolCallCompleted(
                call=call, result=err, is_error=True, output=err
            )
            return

        if tool.requires_approval(args, state.run_ctx):
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
                state.transcript.append(
                    ToolResultEntry(call_id=call.id, output=denial, is_error=True)
                )
                yield events.ToolCallCompleted(
                    call=call, result=denial, is_error=True, output=denial
                )
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
            result_text = await render_tool_result(
                tool, result, state.run_ctx, default=state.agent.tool_result_renderer
            )

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
