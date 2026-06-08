"""Public runner facade.

The user-facing API stays here; mutable orchestration lives in
``lovia.runtime``.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from .runtime.loop import RunLoop
from .agent import Agent
from .checkpointer import Checkpointer, RunSnapshot
from .context import ContextPolicy
from .exceptions import UserError
from .messages import Message, Usage
from .reliability import CancelToken, RetryPolicy, RunBudget
from .run_context import RunContext
from .runtime.result import RunHandle, RunResult
from .session import Session

TContext = TypeVar("TContext")


class Runner:
    """Stateless orchestrator. All entry points are class/static methods."""

    @staticmethod
    def stream(
        agent: Agent[TContext],
        input: str | list[Message],
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
        append_instructions: str | None = None,
        output_type: Any | None = None,
        _parent_usage: Usage | None = None,
    ) -> RunHandle:
        """Start a run and return a :class:`RunHandle`.

        The handle is both awaitable (for the final :class:`RunResult`) and
        async-iterable (for the event stream).
        """
        loop = RunLoop(
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
            append_instructions=append_instructions,
            output_type_override=output_type,
            has_output_type_override=output_type is not None,
        )
        return RunHandle(loop.stream(), loop.approvals)

    @staticmethod
    async def run(
        agent: Agent[TContext],
        input: str | list[Message],
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
        append_instructions: str | None = None,
        output_type: Any | None = None,
        _parent_usage: Usage | None = None,
    ) -> RunResult:
        """Run ``agent`` to completion and return the final result."""
        return await Runner.stream(
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
            append_instructions=append_instructions,
            output_type=output_type,
            _parent_usage=_parent_usage,
        ).result()

    @staticmethod
    def run_sync(
        agent: Agent[TContext],
        input: str | list[Message],
        **kwargs: Any,
    ) -> RunResult:
        """Synchronous wrapper around :meth:`run` for scripts and REPLs."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise UserError(
                "Runner.run_sync() cannot be called from a running event loop",
                hint="Use `await Runner.run(...)` from async code.",
            )
        return asyncio.run(Runner.run(agent, input, **kwargs))

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
    ) -> RunResult:
        """Resume a previously checkpointed run to completion."""
        snapshot = await checkpointer.load(run_id)
        if snapshot is None:
            raise UserError(f"No snapshot found for run_id={run_id!r}")
        return await Runner.run(
            agent,
            input=[],
            context=context,
            max_turns=max_turns,
            budget=budget,
            cancel_token=cancel_token,
            retry=retry,
            checkpointer=checkpointer,
            run_id=run_id,
            resume_from=snapshot,
        )


__all__ = ["Runner", "RunContext", "RunResult", "RunHandle"]
