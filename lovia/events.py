"""Stream events emitted by :meth:`Runner.stream`.

Streaming and observability share the same event types. Events are pure
data — control plumbing (approvals, cancellation) lives elsewhere. See
:mod:`lovia.approvals` for the back-channel used by
:class:`ApprovalRequired`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .transcript import TranscriptEntry
from .messages import ToolCall

if TYPE_CHECKING:
    from .agent import Agent
    from .approvals import ApprovalChannel
    from .parts import ContentPart
    from .runtime.result import RunResult


@dataclass
class Event:
    """Base class for all events. Useful for ``isinstance`` filtering."""


@dataclass
class RunEvent(Event):
    """Base class for run-level lifecycle events."""


@dataclass
class TurnEvent(Event):
    """Base class for per-turn lifecycle events."""


@dataclass
class DeltaEvent(Event):
    """Base class for streamed model deltas."""


@dataclass
class MessageEvent(Event):
    """Base class for completed assistant-message events."""


@dataclass
class ToolEvent(Event):
    """Base class for tool-call and approval events."""


@dataclass
class TransitionEvent(Event):
    """Base class for agent-transition events."""


@dataclass
class ErrorEvent(Event):
    """Base class for error events."""


@dataclass
class ContextEvent(Event):
    """Base class for context-management events."""


@dataclass
class RunStarted(RunEvent):
    agent: "Agent[Any]"


@dataclass
class TurnStarted(TurnEvent):
    agent: "Agent[Any]"
    turn: int


@dataclass
class TurnEnded(TurnEvent):
    agent: "Agent[Any]"
    turn: int


@dataclass
class TextDelta(DeltaEvent):
    """A partial assistant text fragment emitted during streaming."""

    delta: str


@dataclass
class ReasoningDelta(DeltaEvent):
    """A partial chain-of-thought fragment from providers that expose it.

    Surface in your UI as collapsed/secondary text — these fragments are not
    part of the user-visible response and must not be relied on for behavior.
    """

    delta: str


@dataclass
class OutputDiscarded(DeltaEvent):
    """The partial output streamed so far for the current turn was discarded.

    Emitted when the runner recovers from a transient mid-stream error by
    retrying or falling back to another provider (see
    :attr:`~lovia.RetryPolicy.restart_on_partial`). Streamed deltas are
    provisional until :class:`MessageCompleted`; this event invalidates every
    :class:`TextDelta` / :class:`ReasoningDelta` emitted since the turn began.
    A consumer that renders deltas live must clear what it has shown for the
    current turn — a fresh stream that replaces it follows. The persistent
    transcript is unaffected (it is assembled only once the turn completes).
    """


@dataclass
class MessageCompleted(MessageEvent):
    """One assistant turn fully assembled.

    ``entries`` is the slice of new :class:`TranscriptEntry` values produced by that
    turn — typically a :class:`ReasoningEntry`, an :class:`AssistantTextEntry`,
    and any :class:`ToolCallEntry`\\ s the model requested. Subscribers can
    pattern-match on the concrete entry types.
    """

    entries: list[TranscriptEntry]


@dataclass
class UserMessageInjected(MessageEvent):
    """A mid-run injected message, consumed at the start of a turn.

    Emitted when the runner drains a :class:`~lovia.steering.Mailbox` entry and
    appends it to the transcript as a ``user`` message, so a live consumer can
    render the injected turn at the right point in the stream. ``turn`` is the
    turn number at whose start the message was consumed.
    """

    content: "str | list[ContentPart]"
    turn: int


@dataclass
class ToolCallStarted(ToolEvent):
    """Emitted just before a tool actually runs.

    Only fires for calls that reach execution. Calls rejected beforehand —
    unknown tool, malformed arguments, or denied approval — skip straight to
    :class:`ToolCallCompleted` with ``is_error=True`` and never emit this event.

    Tool calls of one turn execute concurrently (unless a tool opts out via
    ``Tool.parallel=False``), so events of *different* calls may interleave;
    correlate by ``ev.call.id``. For any single call, ``ToolCallStarted``
    still precedes its own ``ToolCallCompleted``.
    """

    call: ToolCall


@dataclass
class ToolCallCompleted(ToolEvent):
    """Emitted once a tool call reaches a terminal outcome.

    May arrive **without** a preceding :class:`ToolCallStarted` when the call was
    rejected before execution (see above), so consumers must not assume the two
    pair up. ``is_error`` plus ``result`` distinguish the outcomes (e.g. the
    "is not available" / "Invalid JSON" / "was not approved" messages).
    Completions arrive in completion order, not request order — with parallel
    execution a later-requested call can finish first (see
    :class:`ToolCallStarted` on interleaving).
    """

    call: ToolCall
    result: Any
    """The raw, un-rendered return value — for observability and type-aware
    consumers (e.g. the todo card)."""

    is_error: bool = False
    output: str = ""
    """The rendered result string the model received — the live twin of
    ``ToolResultEntry.output``."""


@dataclass
class HandoffOccurred(TransitionEvent):
    from_agent: "Agent[Any]"
    to_agent: "Agent[Any]"


@dataclass
class ApprovalRequired(ToolEvent):
    """Emitted before a tool that needs approval runs.

    A streaming consumer resolves the request by calling :meth:`approve` or
    :meth:`reject` on the event (any time before its loop yields control
    back to the runner). Out-of-band callers can resolve by ``ToolCall.id``
    via the :class:`~lovia.approvals.ApprovalChannel` accessible from
    :attr:`RunHandle.approvals`. Setting ``Agent.approval_handler`` provides
    a programmatic policy as a third option.

    If none of those paths resolve the request, the runner defaults to
    **deny** so the run cannot hang.

    While the stream is suspended at this event, other tool calls of the same
    turn may still be executing; their events are delivered after the
    consumer resumes the stream.
    """

    call: ToolCall
    # Back-channel reference. Kept private so events stay declarative —
    # callers should prefer ``approve()`` / ``reject()`` on the event or
    # the channel API on ``RunHandle.approvals``.
    _channel: "ApprovalChannel | None" = field(default=None, repr=False)

    def approve(self) -> None:
        """Allow the tool call to proceed."""
        if self._channel is None:
            raise RuntimeError(
                "ApprovalRequired event has no channel attached. "
                "This event was likely constructed outside the runner."
            )
        self._channel.approve(self.call.id)

    def reject(self) -> None:
        """Block the tool call; the model will see a denial message."""
        if self._channel is None:
            raise RuntimeError(
                "ApprovalRequired event has no channel attached. "
                "This event was likely constructed outside the runner."
            )
        self._channel.reject(self.call.id)


@dataclass
class ToolCallFailed(ErrorEvent):
    """A non-terminal error scoped to one tool call.

    Emitted for tool failures, render failures, and approval predicate/handler
    errors; the run continues (the model sees an error result). This event
    carries the actual exception; the paired
    :class:`ToolCallCompleted` (``is_error=True``) carries the string the
    model sees. Terminal run-level failures are :class:`RunFailed` instead.
    """

    error: BaseException
    """The exception raised while processing the call."""

    call: ToolCall | None = None
    """The tool call being processed when the error occurred. Needed to
    attribute an error once one turn's tool events interleave across
    concurrently-executing calls."""


@dataclass
class RunCompleted(RunEvent):
    result: "RunResult"


@dataclass
class RunFailed(RunEvent):
    """Terminal event: the run ended without a result.

    Exactly one of :class:`RunCompleted` / :class:`RunFailed` closes every
    stream — iteration then ends; it never raises. ``error`` is the exception
    the run ended with (:class:`~lovia.exceptions.RunCancelled` for a
    cooperative cancel, :class:`~lovia.exceptions.BudgetExceeded`,
    :class:`~lovia.exceptions.ProviderError`, ...), and
    :meth:`~lovia.RunHandle.result` raises that same exception.
    """

    error: BaseException


@dataclass
class CompactionNotice:
    """A JSON-safe record of one context compaction — what it did, for display.

    Built once by the loop from a :class:`~lovia.context.ContextResult` and the
    reactive flag, then reused three ways: embedded in :class:`ContextCompacted`
    for the live stream, stowed in the finished segment's ``meta`` for the web UI
    to replay on reload, and held on ``RunState.context_notice``. The generic
    fields are typed; ``detail`` is the policy-authored, human-readable tail
    (e.g. ``["context was 85% full", "2 tool results offloaded"]``) that the UI
    renders verbatim — a custom policy fills it however it likes.
    """

    reason: str
    reactive: bool
    summary: str | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None
    detail: list[str] = field(default_factory=list)


@dataclass
class ContextCompacted(ContextEvent):
    """Emitted when :class:`~lovia.ContextPolicy` produced a compacted view.

    ``entries_before`` is the full transcript and remains the source of truth;
    ``entries_after`` is the view the runner sent to the provider for this turn
    only — it is not written back to the Session. ``notice`` is the JSON-safe
    :class:`CompactionNotice` describing what happened (reason, token delta,
    policy-authored detail, and any summary text); the loop stows the same object
    in the finished segment's ``meta`` so the web UI can replay it on reload.
    """

    session_id: str | None
    entries_before: list[TranscriptEntry]
    entries_after: list[TranscriptEntry]
    notice: CompactionNotice


# Deprecated alias (since 0.9): ``ErrorOccurred`` was renamed once ``RunFailed``
# took over the run-level role and this event became tool-scoped. Same class
# object, so ``isinstance`` checks and ``hooks.on`` registrations written
# against either name keep working. Will be removed after one minor release.
ErrorOccurred = ToolCallFailed
