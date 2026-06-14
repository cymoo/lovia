"""Mutable state owned by the run loop.

Three layers, by lifetime:

* :class:`RunState` — everything that changes while a run executes (active
  agent, transcript, resolved tools, turn counter, ...). A handoff mutates
  this in place. It *embeds* the run's :class:`~lovia.run_context.RunContext`
  (the public surface handed to tools/guardrails/hooks) and adds the loop's
  private machinery around it; ``agent`` and ``transcript`` are thin views
  onto the embedded context, not separate storage.
* :class:`ResumeState` — the small, JSON-serializable slice of runner state
  that must survive a checkpoint/resume cycle. It round-trips through
  :attr:`~lovia.checkpointer.RunSnapshot.resume_state`.
* :class:`ModelTurnResult` — scratch for a single model call, populated by
  :func:`~lovia.runtime.model_turn.stream_model_turn` (an async generator
  cannot ``return`` a value, so it fills in an accumulator instead).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .._types import JsonObject
from ..agent import Agent
from ..messages import AssistantTurn
from ..output import StructuredOutput
from ..providers.base import Provider
from ..run_context import RunContext
from ..tools import Tool
from ..transcript import TranscriptEntry, to_json_safe

if TYPE_CHECKING:
    from ..handoff import _HandoffSignal
    from ..hooks import AgentHooks
    from ..plugins import ViewInjector

# Where the run's final output contract came from: the active agent's own
# ``output_type``, or a run-wide ``Runner.run(..., output_type=...)`` override.
OutputTypeSource = Literal["agent", "run_override"]


@dataclass
class ResumeState:
    """Runner-owned accumulators persisted in a checkpoint's ``resume_state``.

    These are live values the loop reads and updates each turn; they are
    grouped here (rather than scattered across :class:`RunState`) because they
    share one trait: each must survive a checkpoint/resume as a JSON-safe unit.

    Attributes:
        last_input_tokens: Input-token count reported by the previous model
            call, so the :class:`~lovia.ContextPolicy` can size compaction
            against real usage instead of the chars/4 heuristic.
        compaction_scratch: Per-run cache the context policy may use for
            derived state (e.g. a running summary). Owned here so it cannot
            leak across runs.
        output_repair_attempts: How many output-repair prompts were already
            appended this run.
        output_type_source: Whether the structured-output contract came from
            the agent or a run-level override (validated on resume).
    """

    last_input_tokens: int | None = None
    compaction_scratch: dict[str, Any] = field(default_factory=dict)
    output_repair_attempts: int = 0
    output_type_source: OutputTypeSource = "agent"

    def to_dict(self) -> JsonObject:
        data = to_json_safe(
            {
                "last_input_tokens": self.last_input_tokens,
                "compaction_scratch": self.compaction_scratch,
                "output_repair_attempts": self.output_repair_attempts,
                "output_type_source": self.output_type_source,
            }
        )
        assert isinstance(data, dict)
        return data

    @classmethod
    def from_dict(cls, data: JsonObject) -> "ResumeState":
        """Rebuild from a snapshot, tolerating missing or malformed keys."""
        state = cls()
        last_input = data.get("last_input_tokens")
        if isinstance(last_input, int) and not isinstance(last_input, bool):
            state.last_input_tokens = last_input
        scratch = data.get("compaction_scratch")
        if isinstance(scratch, dict):
            state.compaction_scratch = dict(scratch)
        attempts = data.get("output_repair_attempts")
        if isinstance(attempts, int) and not isinstance(attempts, bool):
            state.output_repair_attempts = attempts
        source = data.get("output_type_source")
        if source in ("agent", "run_override"):
            state.output_type_source = source
        return state


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

    run_ctx: RunContext[Any]
    tools_by_name: dict[str, Tool]
    structured_output: StructuredOutput | None
    resume_state: ResumeState
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


__all__ = ["ModelTurnResult", "OutputTypeSource", "ResumeState", "RunState"]
