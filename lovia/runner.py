"""Public runner facade.

These three entry points are deliberately thin: they translate keyword
arguments into a :class:`~lovia.runtime.loop.RunLoop` and hand back a
:class:`~lovia.runtime.result.RunHandle`. All orchestration — including loading
a checkpoint and deciding whether to start fresh, resume, or replay — lives in
``lovia.runtime``.
"""

from __future__ import annotations

import asyncio
from typing import Any, TypeVar

from .agent import Agent
from .checkpointer import CheckpointOptions
from .context import ContextPolicy
from .exceptions import UserError
from .messages import Message, Usage
from .reliability import CancelToken, RetryPolicy, RunBudget
from .runtime.loop import RunLoop
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
        output_type: Any = None,
        append_instructions: str | None = None,
        max_turns: int = 50,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        context_policy: ContextPolicy | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        checkpoint: CheckpointOptions | None = None,
        # Framework-internal: sub-agent runs (agent-as-tool) fold their usage
        # into the parent run's accumulator. Not part of the public contract.
        _parent_usage: Usage | None = None,
    ) -> RunHandle:
        """Start a run and return a :class:`RunHandle`.

        The handle is both awaitable (for the final :class:`RunResult`) and
        async-iterable (for the event stream).

        **Idempotent runs.** When ``checkpoint`` is given,
        ``checkpoint.if_run_exists`` decides what happens if that run id already
        has a snapshot — so a crashed worker can just re-issue the same call:

        * ``"resume"`` (default) — continue the existing run (or replay it if it
          already completed); start fresh only if nothing is stored yet. The new
          ``input`` is ignored when an existing run is resumed (the transcript
          already carries it). Treat ``run_id`` as a per-run idempotency key, not
          a session id: reusing a *completed* id replays the old result and
          drops the new ``input``. For conversational continuity use ``session``.
        * ``"restart"`` — ignore any stored run and start fresh, overwriting it.
        * ``"fail"`` — raise if a run already exists under ``run_id``.
        * ``"require"`` — resume an existing run, **raising** if nothing is
          stored. This is how you continue a known run by id without new input::

              async for ev in Runner.stream(
                  agent,
                  [],
                  checkpoint=CheckpointOptions(cp, rid, if_run_exists="require"),
              ):
                  ...

        Resuming a run that already ``completed`` replays it verbatim: the handle
        re-emits the terminal events but does not re-run session persistence,
        output guardrails, or hooks (those ran on the original completion).
        ``session``/``session_id`` are therefore ignored for a completed run.
        """
        loop = RunLoop(
            initial_agent=agent,
            user_input=input,
            context=context,
            output_type_override=output_type,
            append_instructions=append_instructions,
            max_turns=max_turns,
            budget=budget,
            cancel_token=cancel_token,
            retry=retry,
            context_policy=context_policy,
            session=session,
            session_id=session_id,
            checkpoint=checkpoint,
            parent_usage=_parent_usage,
        )
        return RunHandle(loop.stream(), loop.approvals)

    @staticmethod
    async def run(
        agent: Agent[TContext],
        input: str | list[Message],
        *,
        context: TContext | None = None,
        output_type: Any = None,
        append_instructions: str | None = None,
        max_turns: int = 50,
        budget: RunBudget | None = None,
        cancel_token: CancelToken | None = None,
        retry: RetryPolicy | None = None,
        context_policy: ContextPolicy | None = None,
        session: Session | None = None,
        session_id: str | None = None,
        checkpoint: CheckpointOptions | None = None,
        _parent_usage: Usage | None = None,  # framework-internal; see stream()
    ) -> RunResult:
        """Run ``agent`` to completion and return the final result.

        A signature mirror of :meth:`stream` that drives the handle to its
        :class:`RunResult` (see :meth:`stream` for ``if_run_exists`` and the
        idempotent-run semantics). Keep the two parameter lists in sync.
        """
        return await Runner.stream(
            agent,
            input,
            context=context,
            output_type=output_type,
            append_instructions=append_instructions,
            cancel_token=cancel_token,
            budget=budget,
            retry=retry,
            context_policy=context_policy,
            session=session,
            session_id=session_id,
            checkpoint=checkpoint,
            max_turns=max_turns,
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


__all__ = ["Runner", "RunResult", "RunHandle"]
