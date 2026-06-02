"""Tool-call execution and approval handling for the runner."""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .. import events
from ..agent import Agent
from ..approvals import ApprovalChannel
from ..handoff import _HandoffSignal
from ..messages import ToolCall
from ..output import FINAL_OUTPUT_TOOL_NAME, StructuredOutput, parse_structured_output
from ..reliability import CancelToken, RunBudget
from ..run_context import RunContext
from .state import TurnState
from .utils import truncate_repr
from ..tools import Tool, render_tool_result, run_tool
from ..tracing import Tracer
from ..transcript import ToolResultEntry, TranscriptEntry

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
        agent: Agent,
        tools_by_name: dict[str, Tool],
        run_ctx: RunContext[Any],
        tracer: Tracer,
        structured_output: StructuredOutput | None,
        entries: list[TranscriptEntry],
        state: TurnState,
    ) -> AsyncIterator[events.Event]:
        """Execute one assistant-requested tool call."""

        if self.cancel_token is not None:
            self.cancel_token.check()
        if self.budget is not None:
            self.budget.record_tool_call()
            self.budget.check(run_ctx.usage)

        if call.name == FINAL_OUTPUT_TOOL_NAME and structured_output is not None:
            state.final_via_tool = parse_structured_output(
                structured_output, call.arguments
            )
            entries.append(ToolResultEntry(call_id=call.id, output="ok"))
            return

        tool = tools_by_name.get(call.name)
        if tool is None:
            err = f"Tool {call.name!r} is not available."
            entries.append(ToolResultEntry(call_id=call.id, output=err, is_error=True))
            yield events.ToolCallCompleted(call=call, result=err, is_error=True)
            return

        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        if tool.requires_approval(args, run_ctx):
            async for ev in self.await_approval(call, agent, run_ctx):
                yield ev
            approved = self.approvals.register(call.id).result()
            if not approved:
                denial = f"Tool {call.name} was not approved."
                entries.append(
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
                    run_ctx,
                    default_retries=agent.default_tool_retries,
                    default_timeout=agent.default_tool_timeout,
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
            state.handoff_signal = result
            result_text = f"Transferred to {result.target.name}" + (
                f" ({result.reason})" if result.reason else ""
            )
        else:
            result_text = await render_tool_result(
                tool, result, run_ctx, default=agent.tool_result_renderer
            )

        entries.append(
            ToolResultEntry(
                call_id=call.id,
                output=result_text,
                raw=result,
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

    async def await_approval(
        self,
        call: ToolCall,
        agent: Agent,
        run_ctx: RunContext[Any],
    ) -> AsyncIterator[events.Event]:
        """Yield :class:`events.ApprovalRequired` and resolve the channel."""
        fut = self.approvals.register(call.id)
        yield events.ApprovalRequired(call=call, _channel=self.approvals)

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
                    pass
                elif token in ("allow", "approve", "yes"):
                    self.approvals.approve(call.id)
                else:
                    self.approvals.reject(call.id)
            elif not fut.done():
                self.approvals._resolve(call.id, bool(decision))

        if not fut.done():
            self.approvals.reject(call.id)
