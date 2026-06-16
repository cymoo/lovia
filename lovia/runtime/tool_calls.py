"""Tool-call execution and approval handling for the runner."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .. import events
from ..approvals import ApprovalChannel
from ..handoff import _HandoffSignal
from ..messages import ToolCall
from ..reliability import CancelToken, RunBudget
from .run_state import RunState
from .utils import truncate_repr
from ..tools import render_tool_result, run_tool, truncate_tool_output
from ..tracing import Tracer
from ..transcript import ToolResultEntry

logger = logging.getLogger(__name__)


@dataclass
class ToolCallProcessor:
    approvals: ApprovalChannel
    cancel_token: CancelToken | None = None
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

        if self.cancel_token is not None:
            self.cancel_token.check()
        if self.budget is not None:
            # Every requested call counts toward max_tool_calls, including ones
            # rejected just below (unknown tool, malformed args, denied) — so a
            # model stuck spamming a bad tool name still hits the cap.
            self.budget.record_tool_call()
            # TODO: 这个需要check吗，turn开始的检查下应该就够了吧，不求100%准确
            self.budget.check(state.run_ctx.usage)

        tool = state.tools_by_name.get(call.name)
        if tool is None:
            err = f"Tool {call.name!r} is not available."
            state.transcript.append(
                ToolResultEntry(call_id=call.id, output=err, is_error=True)
            )
            yield events.ToolCallCompleted(call=call, result=err, is_error=True)
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
            yield events.ToolCallCompleted(call=call, result=err, is_error=True)
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
                yield events.ToolCallCompleted(call=call, result=denial, is_error=True)
                return

        yield events.ToolCallStarted(call=call)
        logger.info(
            "tool.start: %s call_id=%s args=%s",
            tool.name,
            call.id,
            truncate_repr(args),
        )

        try:
            with tracer.span("tool", name=tool.name, call_id=call.id):
                result = await run_tool(
                    tool,
                    args,
                    state.run_ctx,
                    default_retries=state.agent.default_tool_retries,
                    default_timeout=state.agent.default_tool_timeout,
                )
            is_error = False
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
            result_text = f"Transferred to {result.target.name}" + (
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
        yield events.ToolCallCompleted(call=call, result=result, is_error=is_error)

    def _apply_handler_decision(self, call_id: str, decision: object) -> None:
        if isinstance(decision, str):
            token = decision.strip().lower()
            if token == "ask":
                return  # defer; falls through to the default-deny check
            self.approvals.resolve(call_id, token in ("allow", "approve", "yes"))
        else:
            self.approvals.resolve(call_id, bool(decision))
