"""Mutable state owned by the run loop.

Three layers, by lifetime:

* :class:`ActiveAgent` — everything *derived from the active agent*: its
  resolved provider chain, tool set, structured-output contract, workspace
  session, and plugin contributions. A handoff rebuilds this wholesale and
  swaps it in a single assignment, so per-agent state can never drift apart.
* :class:`RunState` — everything that changes while a run executes but is
  *not* tied to one agent (the transcript, turn counter, token bookkeeping,
  the per-run instruction addendum, ...). It *embeds* the run's
  :class:`~lovia.run_context.RunContext` (the public surface handed to
  tools/guardrails/hooks) and holds the current :class:`ActiveAgent`.
* :class:`ModelTurnResult` — scratch for a single model call, populated by
  :func:`~lovia.runtime.model_turn.stream_model_turn` (an async generator
  cannot ``return`` a value, so it fills in an accumulator instead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..agent import Agent
from ..messages import AssistantTurn
from ..output import StructuredOutput
from ..providers.base import Provider
from ..run_context import RunContext
from ..tools import Tool
from ..transcript import TranscriptEntry

if TYPE_CHECKING:
    from ..guardrails import GuardrailFn
    from ..handoff import _HandoffSignal
    from ..hooks import AgentHooks
    from ..plugins import ViewInjector
    from ..workspace.protocol import WorkspaceSession


@dataclass
class ActiveAgent:
    """All state resolved from the currently active agent.

    Built once at bootstrap and rebuilt wholesale on each handoff (see
    :meth:`RunLoop._resolve_active`). Bundling it means a handoff swaps a single
    field instead of hand-assigning a half-dozen related ones that must stay in
    sync. ``RunContext.agent``/``RunContext.workspace`` mirror ``agent`` and
    ``workspace`` here so user code sees the same active agent and workspace;
    :meth:`RunState.activate` keeps the two in step.

    ``providers`` is the active agent's resolved fallback chain (resolved once
    per agent so HTTP clients are reused across turns; providers built from
    string specs are closed when the run ends, user-supplied instances are left
    to their owner). ``view_injectors`` run every turn to append transient
    entries to the model view; ``plugin_instructions`` are folded into the
    system prompt; ``plugin_hooks`` receive every event alongside the agent's
    own hooks; the plugin guardrails are merged with the agent's own at the
    loop's existing input/output checkpoints (the loop keeps the abort).
    """

    agent: Agent
    providers: list[Provider]
    structured_output: StructuredOutput | None
    tools_by_name: dict[str, Tool]
    workspace: "WorkspaceSession | None" = None
    view_injectors: list["ViewInjector"] = field(default_factory=list)
    plugin_instructions: list[str] = field(default_factory=list)
    plugin_hooks: list["AgentHooks"] = field(default_factory=list)
    plugin_input_guardrails: list["GuardrailFn"] = field(default_factory=list)
    plugin_output_guardrails: list["GuardrailFn"] = field(default_factory=list)


@dataclass
class RunState:
    """Everything that changes while one run executes, minus per-agent state.

    The loop creates this in its bootstrap phase and mutates it in place. The
    split from :class:`~lovia.run_context.RunContext` is by audience: ``run_ctx``
    is the public surface user code (tools, guardrails, hooks) receives;
    everything else here is private loop machinery. State derived from the
    active agent lives in :class:`ActiveAgent` (``active``) and is swapped as a
    unit on handoff; ``agent`` and ``transcript`` are views so the active agent
    and the live transcript have a single source of truth.
    """

    run_ctx: RunContext[Any]
    active: ActiveAgent
    # Persisted to RunSnapshot and restored on resume.
    last_input_tokens: int | None = None
    context_policy_state: dict[str, Any] = field(default_factory=dict)
    # Not persisted; resets on resume (bounded by max_turns).
    output_repair_attempts: int = 0
    turns: int = 0
    # Per-run system-prompt addendum (``extra_instructions``). Run-scoped: it is
    # appended to every active agent's instructions, including agents reached
    # via handoff.
    extra_instructions: str | None = None
    # Set by the tool phase when a handoff tool fired; consumed by the loop.
    pending_handoff: "_HandoffSignal | None" = None

    @property
    def agent(self) -> Agent:
        """The active agent. Mirror of ``active.agent`` / ``run_ctx.agent``."""
        return self.active.agent

    @property
    def transcript(self) -> list[TranscriptEntry]:
        """The live transcript — the very list stored in ``run_ctx.entries``.

        Read/write the list in place (``append``/``extend``/``[:] =``); the
        loop relies on this alias so appended turns reach the model and the
        Session.
        """
        return self.run_ctx.entries

    def activate(self, active: ActiveAgent) -> None:
        """Swap the active agent and mirror its public surface onto ``run_ctx``.

        The single mutation point for a handoff: it replaces all per-agent
        derived state at once and keeps ``RunContext.agent``/``workspace`` (what
        user code sees) in step.
        """
        self.active = active
        self.run_ctx.agent = active.agent
        self.run_ctx.workspace = active.workspace


@dataclass
class ModelTurnResult:
    """Output of one streamed model call."""

    assistant: AssistantTurn | None = None
    turn_entries: list[TranscriptEntry] = field(default_factory=list)


__all__ = ["ActiveAgent", "ModelTurnResult", "RunState"]
