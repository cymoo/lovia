"""Checkpoint persistence for the run loop.

:class:`CheckpointWriter` wraps an optional :class:`~lovia.checkpointer.
Checkpointer` and owns everything snapshot-related: building
:class:`~lovia.checkpointer.RunSnapshot` payloads, classifying terminal
exceptions into resumable vs. final, and the delete-on-success policy. The
loop only says *when* to checkpoint; this module decides *what* gets written.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..types import JsonObject
from ..checkpointer import Checkpointer, RunHead, RunStatus
from ..exceptions import (
    BudgetExceeded,
    MaxTurnsExceeded,
    ProviderError,
    RunCancelled,
)
from ..transcript import to_json_safe
from .run_state import RunState

logger = logging.getLogger(__name__)


@dataclass
class CheckpointWriter:
    """Persists run snapshots when a checkpointer is configured.

    All methods are no-ops when ``checkpointer`` or ``run_id`` is ``None``,
    so the loop can call them unconditionally.

    Failure semantics are deliberately asymmetric. :meth:`save_running` (and
    :meth:`complete`) propagate store errors and thereby **abort the run**:
    checkpointing is a durability guarantee, and silently continuing past a
    failed write would leave a snapshot that lies about what already ran —
    resuming it would re-execute tool calls whose results were lost. Only
    :meth:`save_terminal` is best-effort (logged, never raised), because it
    runs while the original failure is already propagating and must not mask
    it.
    """

    checkpointer: Checkpointer | None
    run_id: str | None
    delete_on_success: bool = False
    # Count of this run's entries already appended to the checkpoint, so each
    # save persists only the new tail. On resume the loop seeds this with the
    # restored snapshot's entry count.
    _persisted: int = 0

    def resume_at(self, persisted: int) -> None:
        """Seed how many of this run's entries are already in the checkpoint.

        Called by the loop on resume so the next ``append`` persists only the
        entries produced after the restored snapshot.
        """
        self._persisted = persisted

    async def delete(self) -> None:
        """Drop this run's snapshot (no-op when checkpointing is off)."""
        if self.checkpointer is not None and self.run_id is not None:
            await self.checkpointer.delete(self.run_id)
        self._persisted = 0

    async def save_running(self, state: RunState) -> None:
        await self._save(state, status="running")

    async def save_terminal(self, state: RunState, exc: BaseException) -> None:
        """Persist an ``interrupted``/``failed`` snapshot for ``exc``.

        Best-effort: a snapshot failure is logged, never raised, so it cannot
        mask the original run failure.
        """
        status = self.classify(exc)
        try:
            await self._save(state, status=status, error=error_payload(exc))
        except Exception:
            logger.exception(
                "checkpoint.%s_snapshot: could not persist run state", status
            )

    async def complete(self, state: RunState, output: object) -> None:
        """Record successful completion (or delete the checkpoint)."""
        if self.checkpointer is None or self.run_id is None:
            return
        if self.delete_on_success:
            await self.checkpointer.delete(self.run_id)
            return
        safe_output = to_json_safe(output)
        error: JsonObject | None = None
        if safe_output is None and output is not None:
            error = {
                "type": "OutputNotSerializable",
                "message": (
                    "Final output could not be serialized into JSON-safe "
                    "checkpoint payload."
                ),
            }
        await self._save(state, status="completed", output=safe_output, error=error)

    @staticmethod
    def classify(exc: BaseException) -> RunStatus:
        """Decide whether a run that ended with ``exc`` can be resumed.

        Cancellation, run limits (turns/budget), transient transport errors,
        and retryable provider errors leave the transcript in a consistent
        state — the run is ``interrupted`` and re-running the same ``run_id``
        may continue it (with raised limits where applicable). Everything else
        is ``failed``.
        """
        if isinstance(
            exc,
            (
                RunCancelled,
                asyncio.CancelledError,
                MaxTurnsExceeded,
                BudgetExceeded,
                TimeoutError,
                ConnectionError,
            ),
        ):
            return "interrupted"
        if isinstance(exc, ProviderError):
            retryable = getattr(exc, "retryable", None) is not False
            return "interrupted" if retryable else "failed"
        return "failed"

    async def _save(
        self,
        state: RunState,
        *,
        status: RunStatus,
        output: object | None = None,
        error: JsonObject | None = None,
    ) -> None:
        """Append this run's new entries and refresh the head. ``output`` must
        already be JSON-safe: ``complete`` pre-serializes it;
        ``save_running``/``save_terminal`` pass ``None``."""
        if self.checkpointer is None or self.run_id is None:
            return
        run_entries = state.run_entries
        delta = run_entries[self._persisted :]
        head = RunHead(
            agent_name=state.agent.name,
            usage=state.run_ctx.usage.clone(),
            turns=state.turns,
            status=status,
            output=output,
            error=error,
            last_input_tokens=state.last_input_tokens,
            context_state=state.context_state,
        )
        await self.checkpointer.append(self.run_id, delta, head)
        self._persisted = len(run_entries)


def error_payload(exc: BaseException) -> JsonObject:
    return {"type": type(exc).__name__, "message": str(exc)}


__all__ = ["CheckpointWriter", "error_payload"]
