"""Mutable state owned by the run loop.

Two layers, by lifetime:

* :class:`RunState` — everything that changes while a run executes (active
  agent, transcript, resolved tools, turn counter, ...). A handoff mutates
  this in place. It *embeds* the run's :class:`~lovia.run_context.RunContext`
  (the public surface handed to tools/guardrails/hooks) and adds the loop's
  private machinery around it; ``agent`` and ``transcript`` are thin views
  onto the embedded context, not separate storage.
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


@dataclass
class RunState:
    """Everything that changes while one run executes.

    The loop creates this in its bootstrap phase and mutates it in place; a
    handoff swaps ``agent``, ``tools_by_name``, ``structured_output``, and
    rewrites ``transcript`` for the new agent.

    The split from :class:`~lovia.run_context.RunContext` is by audience:
    ``run_ctx`` is the public surface user code (tools, guardrails, hooks)
    receives; everything else here is private loop machinery. ``agent`` and
    ``transcript`` are views onto ``run_ctx`` so the active agent and the live
    transcript have a single source of truth.
    """

    # TODO: 这个里面属性太多了，似乎除了某些是必须的：run_ctx, turns, last_input_tokens, context_policy_state等
    # TODO: 其他可以收敛吗，比如新增一个current_agent，其他属性可以从它计算而来

    run_ctx: RunContext[Any]
    tools_by_name: dict[str, Tool]
    structured_output: StructuredOutput | None
    # Persisted to RunSnapshot and restored on resume.
    last_input_tokens: int | None = None
    context_policy_state: dict[str, Any] = field(default_factory=dict)
    # Not persisted; resets on resume (bounded by max_turns).
    output_repair_attempts: int = 0
    # The active agent's resolved provider fallback chain. Resolved once per
    # agent (at bootstrap and on each handoff) so HTTP clients are reused
    # across turns; providers built from string specs are closed when the run
    # ends, user-supplied Provider instances are left to their owner.
    providers: list[Provider] = field(default_factory=list)
    turns: int = 0
    # Per-call system-prompt addendum (``append_instructions``). Applied to
    # the initial agent only; cleared on the first handoff so subsequent
    # agents use their own instructions verbatim.
    system_extra: str | None = None
    # Set by the tool phase when a handoff tool fired; consumed by the loop.
    pending_handoff: "_HandoffSignal | None" = None
    # Per-run plugin contributions (rebuilt at bootstrap and on each handoff).
    # ``view_injectors`` run every turn to append transient entries to the
    # model view; ``plugin_instructions`` are folded into the system prompt;
    # ``plugin_hooks`` receive every event alongside the agent's own hooks.
    view_injectors: list["ViewInjector"] = field(default_factory=list)
    plugin_instructions: list[str] = field(default_factory=list)
    plugin_hooks: list["AgentHooks"] = field(default_factory=list)
    # Guardrails contributed by plugins, merged with the agent's own at the
    # loop's existing input/output checkpoints (the loop keeps the abort).
    plugin_input_guardrails: list["GuardrailFn"] = field(default_factory=list)
    plugin_output_guardrails: list["GuardrailFn"] = field(default_factory=list)

    @property
    def agent(self) -> Agent:
        """The active agent. Single source of truth: ``run_ctx.agent``."""
        return self.run_ctx.agent

    @agent.setter
    def agent(self, value: Agent) -> None:
        self.run_ctx.agent = value

    @property
    def transcript(self) -> list[TranscriptEntry]:
        """The live transcript — the very list stored in ``run_ctx.entries``.

        Read/write the list in place (``append``/``extend``/``[:] =``); the
        loop relies on this alias so appended turns reach the model and the
        Session.
        """
        return self.run_ctx.entries


@dataclass
class ModelTurnResult:
    """Output of one streamed model call."""

    assistant: AssistantTurn | None = None
    turn_entries: list[TranscriptEntry] = field(default_factory=list)


__all__ = ["ModelTurnResult", "RunState"]
