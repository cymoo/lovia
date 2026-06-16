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
# * ``resume_only`` — continue an existing run, else raise (resume a known run_id).
def check_resumable(agent: Agent, snapshot: RunSnapshot) -> None:
    """Gate a resume against ``agent`` before any work happens.

    # TODO: the comments below are stale
    Raises :class:`UserError` if the snapshot belongs to a different agent.
    A ``completed`` snapshot passes this gate; the caller short-circuits it
    separately. ``failed`` snapshots are allowed through — the underlying
    cause may have been fixed by the caller (e.g. a permission error after
    the user corrected directory access).
    """
    _validate_snapshot_agent(agent, snapshot)


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


# TODO: 似乎有个BUG
# 如果发生了handoff，snapshot中agent_name就变了，无法通过验证
def _validate_snapshot_agent(agent: Agent, snapshot: RunSnapshot) -> None:
    if snapshot.agent_name == agent.name:
        return
    raise UserError(
        f"Snapshot {snapshot.run_id!r} belongs to active agent "
        f"{snapshot.agent_name!r}, not {agent.name!r}.",
        hint="Resume this run_id with the agent recorded in the checkpoint.",
    )


__all__ = [
    "check_resumable",
    "IfRunExists",
    "result_from_completed_snapshot",
]
