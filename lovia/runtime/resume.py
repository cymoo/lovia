"""Resume-side helpers: validate and rehydrate a checkpointed run.

The write side (persisting snapshots) lives in :mod:`lovia.runtime.checkpoint`.
This module is its counterpart — the pure functions :class:`RunLoop` uses to
gate a resume and to reconstruct the terminal result of an already-completed
run. Keeping them here (rather than in the public facade) lets the loop own the
whole start-vs-resume decision without a facade <-> runtime import cycle.
"""

from __future__ import annotations

from typing import Any

from ..agent import Agent
from ..checkpointer import IfRunExists, RunSnapshot
from ..exceptions import UserError
from ..schema import coerce_output
from .result import RunResult


# Policy for ``stream``/``run`` when ``run_id`` already has a snapshot in the
# checkpointer. ``run_id`` is meant as a per-run idempotency key (not a session
# id), so the default continues an existing run rather than duplicating it.
#
# * ``resume``  — continue an existing run, else start fresh (default).
# * ``restart`` — ignore any stored run and start fresh, overwriting it.
# * ``fail``    — raise if a run already exists.
# * ``require`` — continue an existing run, else raise (resume a known run_id).
def check_resumable(agent: Agent, snapshot: RunSnapshot, *, output_type: Any) -> None:
    """Gate a resume against ``agent`` before any work happens.

    Raises :class:`UserError` if the snapshot belongs to a different agent, is
    in a ``failed`` state, or was created with a run-level ``output_type`` the
    caller did not re-supply. A ``completed`` snapshot passes this gate; the
    caller short-circuits it separately.
    """
    _validate_snapshot_agent(agent, snapshot)
    if snapshot.status == "failed":
        _raise_failed_snapshot(snapshot)
    _validate_snapshot_output_type(snapshot, output_type=output_type)


def result_from_completed_snapshot(
    agent: Agent,
    snapshot: RunSnapshot,
    *,
    output_type: Any = None,
) -> RunResult:
    """Rebuild the :class:`RunResult` of an already-completed snapshot."""
    target_output_type = output_type if output_type is not None else agent.output_type
    output = snapshot.output
    if output is None and (snapshot.error or {}).get("type") == "OutputNotSerializable":
        raise UserError(
            f"Checkpoint {snapshot.run_id!r} completed, but its output is not JSON-safe.",
            hint="Rerun the task or use `CheckpointOptions(..., delete_on_success=True)` for non-serializable outputs.",
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


def _validate_snapshot_agent(agent: Agent, snapshot: RunSnapshot) -> None:
    if snapshot.agent_name == agent.name:
        return
    raise UserError(
        f"Snapshot {snapshot.run_id!r} belongs to active agent "
        f"{snapshot.agent_name!r}, not {agent.name!r}.",
        hint="Resume this run_id with the agent recorded in the checkpoint.",
    )


def _raise_failed_snapshot(snapshot: RunSnapshot) -> None:
    err = snapshot.error or {}
    error_type = err.get("type", "error")
    message = err.get("message", "Run failed before completion.")
    raise UserError(
        f"Checkpoint {snapshot.run_id!r} is failed ({error_type}: {message}).",
        hint="Start a new run or inspect the checkpoint error payload.",
    )


def _validate_snapshot_output_type(snapshot: RunSnapshot, *, output_type: Any) -> None:
    # Enforces that a run-level output_type is *re-supplied* on resume; it does
    # not check the supplied type matches the original. A mismatched type just
    # re-coerces the stored output (and may fail in coerce_output).
    if output_type is not None:
        return
    if snapshot.resume_state.get("output_type_source") != "run_override":
        return
    raise UserError(
        f"Checkpoint {snapshot.run_id!r} was created with a run-level output_type.",
        hint="Pass the same `output_type=` when resuming this run_id with Runner.run/stream.",
    )


__all__ = [
    "IfRunExists",
    "check_resumable",
    "result_from_completed_snapshot",
]
