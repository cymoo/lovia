"""Public runner facade.

The user-facing API stays here; mutable orchestration lives in
``lovia.runtime``.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, TypeVar

from . import events
from .runtime.loop import RunLoop
from .agent import Agent
from .approvals import ApprovalChannel
from .checkpointer import Checkpointer, RunSnapshot
from .context import ContextPolicy
from .exceptions import UserError
from .messages import Message, Usage
from .schema import coerce_output
from .reliability import CancelToken, RetryPolicy, RunBudget
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
        output_type: Any | None = None,
        append_instructions: str | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        checkpointer: Checkpointer | None = None,
        run_id: str | None = None,
        resume_from: RunSnapshot | None = None,
        delete_checkpoint_on_success: bool = False,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        context_policy: ContextPolicy | None = None,
        _parent_usage: Usage | None = None,
    ) -> RunHandle:
        """Start a run and return a :class:`RunHandle`.

        The handle is both awaitable (for the final :class:`RunResult`) and
        async-iterable (for the event stream).
        """
        if resume_from is not None:
            _validate_snapshot_agent(agent, resume_from)
            if resume_from.status == "failed":
                _raise_failed_snapshot(resume_from)
            _validate_snapshot_output_type(resume_from, output_type=output_type)
            if resume_from.status == "completed":
                return RunHandle(
                    _completed_snapshot_stream(
                        agent,
                        resume_from,
                        output_type=output_type,
                        parent_usage=_parent_usage,
                        checkpointer=checkpointer,
                        delete_checkpoint_on_success=delete_checkpoint_on_success,
                    ),
                    ApprovalChannel(),
                )
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
            delete_checkpoint_on_success=delete_checkpoint_on_success,
        )
        return RunHandle(loop.stream(), loop.approvals)

    @staticmethod
    async def run(
        agent: Agent[TContext],
        input: str | list[Message],
        *,
        context: TContext | None = None,
        output_type: Any | None = None,
        append_instructions: str | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        checkpointer: Checkpointer | None = None,
        run_id: str | None = None,
        resume_from: RunSnapshot | None = None,
        delete_checkpoint_on_success: bool = False,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        context_policy: ContextPolicy | None = None,
        _parent_usage: Usage | None = None,
    ) -> RunResult:
        """Run ``agent`` to completion and return the final result."""
        return await Runner.stream(
            agent,
            input,
            context=context,
            output_type=output_type,
            append_instructions=append_instructions,
            session=session,
            session_id=session_id,
            checkpointer=checkpointer,
            run_id=run_id,
            resume_from=resume_from,
            delete_checkpoint_on_success=delete_checkpoint_on_success,
            max_turns=max_turns,
            budget=budget,
            cancel_token=cancel_token,
            retry=retry,
            context_policy=context_policy,
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
        output_type: Any | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        delete_checkpoint_on_success: bool = False,
        max_turns: int = 20,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        context_policy: ContextPolicy | None = None,
    ) -> RunResult:
        """Resume a previously checkpointed run to completion.

        If the original run persisted to a :class:`Session`, pass the same
        ``session``/``session_id`` here so the resumed run's transcript is
        written back on completion.
        """
        snapshot = await checkpointer.load(run_id)
        if snapshot is None:
            raise UserError(f"No snapshot found for run_id={run_id!r}")
        _validate_snapshot_agent(agent, snapshot)
        if snapshot.status == "failed":
            _raise_failed_snapshot(snapshot)
        _validate_snapshot_output_type(snapshot, output_type=output_type)
        if snapshot.status == "completed":
            result = _result_from_completed_snapshot(
                agent,
                snapshot,
                output_type=output_type,
            )
            if delete_checkpoint_on_success:
                await checkpointer.delete(run_id)
            return result
        return await Runner.run(
            agent,
            input=[],
            context=context,
            output_type=output_type,
            session=session,
            session_id=session_id,
            checkpointer=checkpointer,
            run_id=run_id,
            resume_from=snapshot,
            delete_checkpoint_on_success=delete_checkpoint_on_success,
            max_turns=max_turns,
            budget=budget,
            cancel_token=cancel_token,
            retry=retry,
            context_policy=context_policy,
        )


async def _completed_snapshot_stream(
    agent: Agent[TContext],
    snapshot: RunSnapshot,
    *,
    output_type: Any | None,
    parent_usage: Usage | None,
    checkpointer: Checkpointer | None,
    delete_checkpoint_on_success: bool,
) -> AsyncIterator[events.Event]:
    result = _result_from_completed_snapshot(
        agent,
        snapshot,
        output_type=output_type,
    )
    if parent_usage is not None:
        parent_usage.add(result.usage)
    if delete_checkpoint_on_success and checkpointer is not None:
        await checkpointer.delete(snapshot.run_id)
    yield events.RunStarted(agent=agent)
    yield events.RunCompleted(result=result)


def _validate_snapshot_agent(agent: Agent[TContext], snapshot: RunSnapshot) -> None:
    if snapshot.agent_name == agent.name:
        return
    raise UserError(
        f"Snapshot {snapshot.run_id!r} belongs to active agent "
        f"{snapshot.agent_name!r}, not {agent.name!r}.",
        hint="Pass the active agent recorded in the checkpoint to Runner.resume().",
    )


def _raise_failed_snapshot(snapshot: RunSnapshot) -> None:
    err = snapshot.error or {}
    error_type = err.get("type", "error")
    message = err.get("message", "Run failed before completion.")
    raise UserError(
        f"Checkpoint {snapshot.run_id!r} is failed ({error_type}: {message}).",
        hint="Start a new run or inspect the checkpoint error payload.",
    )


def _validate_snapshot_output_type(
    snapshot: RunSnapshot, *, output_type: Any | None
) -> None:
    if output_type is not None:
        return
    if snapshot.resume_state.get("output_type_source") != "run_override":
        return
    raise UserError(
        f"Checkpoint {snapshot.run_id!r} was created with a run-level output_type.",
        hint="Pass the same `output_type=` to Runner.resume() or Runner.run(..., resume_from=...).",
    )


def _result_from_completed_snapshot(
    agent: Agent[TContext],
    snapshot: RunSnapshot,
    *,
    output_type: Any | None = None,
) -> RunResult:
    target_output_type = output_type if output_type is not None else agent.output_type
    output = snapshot.output
    if output is None and (snapshot.error or {}).get("type") == "OutputNotSerializable":
        raise UserError(
            f"Checkpoint {snapshot.run_id!r} completed, but its output is not JSON-safe.",
            hint="Rerun the task or use `delete_checkpoint_on_success=True` for non-serializable outputs.",
        )
    if target_output_type is not str:
        output = coerce_output(target_output_type, output)
    elif output is None:
        output = ""
    return RunResult(
        output=output,
        entries=list(snapshot.entries),
        final_agent=agent,
        usage=snapshot.usage.clone(),
        turns=snapshot.turns,
    )


__all__ = ["Runner", "RunResult", "RunHandle"]
