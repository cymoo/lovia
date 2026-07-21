"""Resume-side helpers: validate and rehydrate a checkpointed run.

The write side (persisting snapshots) lives in :mod:`lovia.runtime.checkpoint`.
This module is its counterpart — the pure functions :class:`RunLoop` uses to
resolve which agent a resume continues as and to reconstruct the terminal
result of an already-completed run. Keeping them here (rather than in the public
facade) lets the loop own the whole start-vs-resume decision without a facade
<-> runtime import cycle.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from typing import Any

from ..agent import Agent
from ..checkpointer import IfRunExists, RunSnapshot
from ..exceptions import UserError
from ..handoff import Handoff
from ..schema import coerce_output
from ..transcript import ToolCallEntry, ToolResultEntry, TranscriptEntry
from .result import RunResult
from .tool_calls import wire_safe_arguments


# Policy for ``stream``/``run`` when ``run_id`` already has a snapshot in the
# checkpointer. ``run_id`` is meant as a per-run idempotency key (not a session
# id), so the default continues an existing run rather than duplicating it.
#
# * ``resume``  — continue an existing run, else start fresh (default).
# * ``restart`` — ignore any stored run and start fresh, overwriting it.
# * ``fail``    — raise if a run already exists.
# * ``resume_only`` — continue an existing run, else raise (resume a known run_id).


def normalize_replayed_entries(
    entries: list[TranscriptEntry],
) -> list[TranscriptEntry]:
    """Wire-safe copies of stored entries, for rehydrating a snapshot.

    The live loop normalizes a rejected call's transcript entry in memory,
    but the checkpoint had already persisted the original bytes — saves are
    append-only and the entry was written with its model turn, before the
    rejection ran. A snapshot can therefore carry non-wire-safe ``arguments``
    that every later request would re-send verbatim: invalid JSON 400s
    OpenAI-dialect endpoints, and the Anthropic serializer's ``json.loads``
    raises outright. Normalizing at the load boundary is idempotent, so every
    load also self-heals snapshots persisted before this existed.

    Calls **without** a matching result are left untouched: the resume drain
    is about to re-execute them, and its preflight rejection both produces
    the right model-facing error for the raw payload and normalizes the
    entry itself. Duplicate ids pair by occurrence, mirroring
    :func:`~lovia.runtime.loop.pending_tool_calls`.
    """
    unconsumed = Counter(
        entry.call_id for entry in entries if isinstance(entry, ToolResultEntry)
    )
    out: list[TranscriptEntry] = []
    for entry in entries:
        if isinstance(entry, ToolCallEntry) and unconsumed[entry.call_id] > 0:
            unconsumed[entry.call_id] -= 1
            normalized = wire_safe_arguments(entry.arguments)
            if normalized is not None:
                out.append(replace(entry, arguments=normalized))
                continue
        out.append(entry)
    return out


def reachable_agents(entry: Agent[Any]) -> dict[str, Agent[Any]]:
    """Map every agent reachable from ``entry`` via handoffs, keyed by name.

    Walks the static handoff graph (``Agent.handoffs``, each item an ``Agent``
    or a :class:`~lovia.handoff.Handoff`), following targets transitively and
    guarding against cycles. ``entry`` itself is always included. When two
    distinct agents share a name the first reached wins — the same ambiguity
    already affects ``transfer_to_<name>`` tool naming.
    """
    found: dict[str, Agent[Any]] = {}
    stack = [entry]
    while stack:
        agent = stack.pop()
        if agent.name in found:
            continue
        found[agent.name] = agent
        for h in agent.handoffs:
            stack.append(h.target if isinstance(h, Handoff) else h)
    return found


def resolve_resume_agent(entry: Agent[Any], snapshot: RunSnapshot) -> Agent[Any]:
    """Resolve the agent a resumed run must continue as.

    A run is always resumed by passing the **entry** agent to the runner, but
    the snapshot records whichever agent was *active* when it was written —
    after a handoff that is a different agent. Find it by name in the entry
    agent's reachable handoff graph so the rebuilt run continues with the right
    tools, providers, and system prompt.

    Raises :class:`UserError` when the snapshot's active agent is not reachable
    from ``entry`` (resuming with the wrong entry agent, or a handoff graph that
    changed since the snapshot was written). A ``completed`` snapshot resolves
    here too, but its caller downgrades this error to a warning and falls back
    to the entry agent — replay only needs attribution, whereas continuing
    *execution* as the wrong agent is dangerous. ``failed`` snapshots are
    allowed through — the underlying cause may have been fixed by the caller
    (e.g. a permission error corrected after the fact).
    """
    agents = reachable_agents(entry)
    active = agents.get(snapshot.agent_name)
    if active is None:
        raise UserError(
            f"Snapshot {snapshot.run_id!r} was last active on agent "
            f"{snapshot.agent_name!r}, which is not reachable from the handoff "
            f"graph of entry agent {entry.name!r}.",
            hint=(
                "Resume this run_id with the same entry agent you started it "
                "with (the one whose handoffs reach the recorded agent)."
            ),
        )
    return active


def result_from_completed_snapshot(
    agent: Agent[Any],
    snapshot: RunSnapshot,
    *,
    output_type: Any = None,
) -> RunResult:
    """Rebuild the :class:`RunResult` of an already-completed snapshot.

    ``agent`` is the snapshot's *active* agent (resolved via
    :func:`resolve_resume_agent`), so ``final_agent`` and the output-type
    coercion reflect the agent that actually finished the run.
    ``finish_reason`` is not persisted in snapshots, so a replayed result
    reports ``None`` there.
    """
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
        # Normalized: these entries feed the replay path's session heal, so
        # non-wire-safe arguments persisted before normalization must not be
        # re-published into the conversation history.
        entries=normalize_replayed_entries(list(snapshot.entries)),
        final_agent=agent,
        usage=snapshot.usage.clone(),
        turns=snapshot.turns,
        last_input_tokens=snapshot.last_input_tokens,
    )


__all__ = [
    "IfRunExists",
    "normalize_replayed_entries",
    "reachable_agents",
    "resolve_resume_agent",
    "result_from_completed_snapshot",
]
