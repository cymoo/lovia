"""Transcript entries: the canonical records that make up a run transcript.

``TranscriptEntry`` is what lovia uses internally to represent the transcript
and what the public API surfaces in ``RunResult.entries``, hooks, and events.
Each entry is a small typed record; provider adapters translate between
entries and their wire format (OpenAI Chat ``messages``, Anthropic Messages,
...) so the runner core stays provider-agnostic.

Entries are deliberately implemented as plain ``dataclass``\\ es rather than
Pydantic models — they sit on the hot path (one per assistant token batch, one
per tool call) and we want minimal overhead and easy ``match`` ergonomics.
``entry_to_dict`` / ``entry_from_dict`` handle (de)serialization for sessions and
checkpoints; the ``type`` field acts as the discriminator.

Streaming providers usually emit display deltas that the runner assembles into
entries at end-of-turn. Providers can also emit
:class:`EntryCompletedDelta` when the final provider-native entry carries ids
or metadata that would otherwise be lost.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from typing import Any, Callable, Literal, Union

from ._types import JsonObject
from .content import ContentPart, FilePart, ImagePart, TextPart
from .messages import AssistantTurn, Message, ToolCall, Usage

# ---------------------------------------------------------------------------
# Transcript entries
# ---------------------------------------------------------------------------


@dataclass
class InputEntry:
    """A ``system`` or ``user`` message contributed by the caller.

    Shape is identical for both roles, so we use a single class with a
    ``role`` field instead of two near-duplicates. ``content`` follows the
    same convention as :class:`Message`: a plain string for the common
    case, or a list of typed :class:`ContentPart`\\ s for multimodal input.
    """

    role: Literal["system", "user"]
    content: str | list[ContentPart]
    type: Literal["input"] = "input"


@dataclass
class AssistantTextEntry:
    """A textual assistant response.

    ``id`` is set when the provider assigns a stable identifier; it is ``None``
    for Chat Completions and most other providers.
    """

    content: str
    id: str | None = None
    type: Literal["assistant_text"] = "assistant_text"


@dataclass
class ReasoningEntry:
    """Provider-scoped reasoning state that may need replay on later turns.

    ``content`` is the display/search text, when the provider exposes one.
    Provider-private replay data (signatures, encrypted reasoning, etc.) lives
    in ``metadata`` and is only interpreted by the provider that wrote it.
    """

    content: str
    id: str | None = None
    provider: str | None = None
    metadata: JsonObject = field(default_factory=dict)
    type: Literal["reasoning"] = "reasoning"


@dataclass
class ToolCallEntry:
    """A function-tool call the model wants to invoke.

    ``arguments`` is the raw JSON string as emitted by the model — we keep
    it unparsed so error messages can quote the exact payload.
    """

    call_id: str
    name: str
    arguments: str
    type: Literal["tool_call"] = "tool_call"


@dataclass
class ToolResultEntry:
    """The (already-rendered) result of executing a :class:`ToolCallEntry`.

    ``output`` is the string the model will see. ``raw`` preserves the
    Python-side return value when the tool returned something structured
    so hooks and renderers can inspect it later without re-parsing.
    """

    call_id: str
    output: str
    raw: object | None = None
    is_error: bool = False
    type: Literal["tool_result"] = "tool_result"


TranscriptEntry = Union[
    InputEntry,
    AssistantTextEntry,
    ReasoningEntry,
    ToolCallEntry,
    ToolResultEntry,
]
"""Discriminated union of every entry kind that can appear in a transcript.

Handoffs reuse :class:`ToolCallEntry` / :class:`ToolResultEntry`; we
add specialised types only when a provider exposes a structurally
distinct concept that we cannot lossily flatten.
"""


# ---------------------------------------------------------------------------
# Streaming deltas
# ---------------------------------------------------------------------------


@dataclass
class TextDelta:
    """Incremental text for the current assistant message."""

    text: str
    type: Literal["text_delta"] = "text_delta"


@dataclass
class ReasoningDelta:
    """Incremental reasoning text."""

    text: str
    type: Literal["reasoning_delta"] = "reasoning_delta"


@dataclass
class ToolCallDelta:
    """Incremental tool call.

    Providers stream tool calls one fragment at a time. ``index`` identifies
    which parallel call the fragment belongs to. ``call_id`` / ``name`` are
    typically set on the first chunk; subsequent chunks only carry
    ``arguments``.
    """

    index: int
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""
    type: Literal["tool_call_delta"] = "tool_call_delta"


@dataclass
class UsageDelta:
    """Final usage report, typically emitted once at end-of-turn."""

    usage: Usage
    type: Literal["usage_delta"] = "usage_delta"


@dataclass
class FinishDelta:
    """End-of-turn marker with the provider's reported finish reason."""

    reason: str | None = None
    type: Literal["finish_delta"] = "finish_delta"


@dataclass
class EntryCompletedDelta:
    """A provider-authoritative transcript entry that has finished streaming.

    Most providers stream incremental deltas (TextDelta, ReasoningDelta,
    ToolCallDelta) and the runner reconstructs transcript entries from those
    fragments at the end of the turn.

    Some providers, however, expose additional information only when a
    content block is fully completed, such as:

    - provider-assigned ids
    - signatures
    - encrypted reasoning state
    - provider-specific metadata
    - finalized tool-call payloads

    Such information cannot always be reconstructed losslessly from deltas
    alone. In those cases the provider emits EntryCompletedDelta containing
    the exact TranscriptEntry that should appear in the transcript.

    The runner treats this entry as authoritative. When a completed entry of
    a given type (ReasoningEntry, AssistantTextEntry, ToolCallEntry, etc.)
    is present, it takes precedence over entries reconstructed from streamed
    deltas.

    Note:
        Despite the name, this is not an incremental delta. It is a stream
        event indicating that a transcript entry has been fully completed and
        finalized by the provider.

    Example:
        Anthropic thinking streams:

            ReasoningDelta("I")
            ReasoningDelta(" am")
            ReasoningDelta(" thinking")

        can later be finalized as:

            EntryCompletedDelta(
                ReasoningEntry(
                    content="I am thinking",
                    provider="anthropic",
                    metadata={"signature": "..."},
                )
            )

        allowing provider-specific metadata to be preserved in the transcript.
    """

    entry: TranscriptEntry
    type: Literal["entry_completed_delta"] = "entry_completed_delta"


ModelDelta = Union[
    TextDelta,
    ReasoningDelta,
    ToolCallDelta,
    UsageDelta,
    FinishDelta,
    EntryCompletedDelta,
]
"""Discriminated union of streaming deltas a provider may yield."""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


_ENTRY_TYPES: dict[str, Callable[..., TranscriptEntry]] = {
    "input": InputEntry,
    "assistant_text": AssistantTextEntry,
    "reasoning": ReasoningEntry,
    "tool_call": ToolCallEntry,
    "tool_result": ToolResultEntry,
}


def entry_to_dict(entry: TranscriptEntry) -> JsonObject:
    """Serialize a :class:`TranscriptEntry` to a JSON-safe ``dict``.

    Handles :class:`InputEntry` specially because ``ContentPart``
    dataclasses also need their ``type`` discriminator preserved.
    :class:`ToolResultEntry` is handled specially because its ``raw`` field may
    hold arbitrary Python objects (e.g. Pydantic models). We preserve a
    JSON-safe version when possible and fall back to ``None``.
    ``dataclasses.asdict`` does the right thing for everything else.
    """
    if isinstance(entry, InputEntry):
        return {
            "type": entry.type,
            "role": entry.role,
            "content": _content_to_dict(entry.content),
        }
    if isinstance(entry, ToolResultEntry):
        return {
            "type": entry.type,
            "call_id": entry.call_id,
            "output": entry.output,
            "is_error": entry.is_error,
            "raw": to_json_safe(entry.raw),
        }
    return asdict(entry)


def to_json_safe(value: object) -> Any:
    """Return a JSON-safe version of ``value``, or ``None`` if impossible."""
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "model_dump"):
        try:
            return to_json_safe(value.model_dump(mode="json"))  # type: ignore[attr-defined]
        except Exception:
            return None
    if is_dataclass(value) and not isinstance(value, type):
        return to_json_safe(asdict(value))
    if isinstance(value, Mapping):
        dict_out: dict[str, Any] = {}
        for key, item in value.items():
            safe = to_json_safe(item)
            if safe is None and item is not None:
                return None
            dict_out[str(key)] = safe
        return dict_out
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        list_out: list[Any] = []
        for item in value:
            safe = to_json_safe(item)
            if safe is None and item is not None:
                return None
            list_out.append(safe)
        return list_out
    return None


def entry_from_dict(data: dict[str, Any]) -> TranscriptEntry:
    """Deserialize a :class:`TranscriptEntry` produced by :func:`entry_to_dict`.

    Raises ``ValueError`` on unknown discriminator — callers are expected
    to have a stable schema (we don't silently drop unknown fields, since
    that's how reasoning entries get lost).
    """
    type_ = data.get("type")
    if type_ not in _ENTRY_TYPES:
        raise ValueError(f"Unknown entry type: {type_!r}")
    cls = _ENTRY_TYPES[type_]
    if cls is InputEntry:
        return InputEntry(
            role=data["role"],
            content=_content_from_dict(data["content"]),
        )
    # Strip ``type`` since it's the dataclass default; pass everything else
    # through so we surface unknown fields as a clean TypeError.
    payload = {k: v for k, v in data.items() if k != "type"}
    return cls(**payload)


def _content_to_dict(
    content: str | list[ContentPart],
) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    return [asdict(part) for part in content]


def _content_from_dict(content: Any) -> str | list[ContentPart]:
    if isinstance(content, str):
        return content
    parts: list[ContentPart] = []
    for raw in content:
        t = raw.get("type")
        if t == "text":
            parts.append(TextPart(text=raw["text"]))
        elif t == "image":
            parts.append(
                ImagePart(
                    url=raw.get("url"),
                    data=raw.get("data"),
                    mime_type=raw.get("mime_type"),
                    detail=raw.get("detail"),
                )
            )
        elif t == "file":
            parts.append(
                FilePart(
                    url=raw.get("url"),
                    data=raw.get("data"),
                    mime_type=raw.get("mime_type"),
                    filename=raw.get("filename"),
                )
            )
        else:
            raise ValueError(f"Unknown content part type: {t!r}")
    return parts


# ---------------------------------------------------------------------------
# Message ↔ TranscriptEntry conversions
# Message is a lossy, chat-provider-shaped view; TranscriptEntry is the
# canonical run history.
# ---------------------------------------------------------------------------


def assistant_to_entries(am: AssistantTurn) -> list[TranscriptEntry]:
    """Split an :class:`AssistantTurn` into transcript entries.

    Order matches the conceptual emission order: message body, then tool calls.
    Empty / absent fields are skipped — a tool-only turn produces just the
    ``ToolCallEntry``\\ s with no preceding assistant text entry.
    """
    out: list[TranscriptEntry] = []
    if am.content:
        out.append(AssistantTextEntry(content=am.content))
    for tc in am.tool_calls:
        out.append(ToolCallEntry(call_id=tc.id, name=tc.name, arguments=tc.arguments))
    return out


def input_to_entries(messages: list[Message]) -> list[TranscriptEntry]:
    """Translate a system/user message prefix to :class:`InputEntry`\\ s.

    Only ``system`` and ``user`` roles are accepted — the runner uses this to
    seed the run transcript. Assistant / tool messages
    in a snapshot are handled by :func:`messages_to_entries` instead.
    """
    out: list[TranscriptEntry] = []
    for m in messages:
        if m.role not in ("system", "user"):
            raise ValueError(f"input_to_entries: unexpected role {m.role!r}")
        content = m.content if m.content is not None else ""
        out.append(InputEntry(role=m.role, content=content))  # type: ignore[arg-type]
    return out


def messages_to_entries(messages: list[Message]) -> list[TranscriptEntry]:
    """Translate a chat-format transcript (any roles) back to entries.

    Used when resuming from a snapshot. ``tool`` messages have no ``raw``
    return value to recover, so ``ToolResultEntry.raw`` is left ``None``.
    """
    out: list[TranscriptEntry] = []
    for m in messages:
        if m.role in ("system", "user"):
            content = m.content if m.content is not None else ""
            out.append(InputEntry(role=m.role, content=content))  # type: ignore[arg-type]
        elif m.role == "assistant":
            if m.reasoning:
                out.append(ReasoningEntry(content=m.reasoning))
            if m.content:
                # ``content`` may be a list[ContentPart]; flatten to text for
                # the AssistantTextEntry (richer shapes are 9d territory).
                from .content import text_of

                out.append(AssistantTextEntry(content=text_of(m.content)))
            for tc in m.tool_calls:
                out.append(
                    ToolCallEntry(call_id=tc.id, name=tc.name, arguments=tc.arguments)
                )
        elif m.role == "tool":
            from .content import text_of

            out.append(
                ToolResultEntry(
                    call_id=m.tool_call_id or "",
                    output=text_of(m.content),
                )
            )
        else:  # pragma: no cover - defensive
            raise ValueError(f"messages_to_entries: unknown role {m.role!r}")
    return out


def entries_to_messages(entries: list[TranscriptEntry]) -> list[Message]:
    """Inverse of :func:`messages_to_entries`.

    Groups consecutive assistant-side text/tool-call entries into one
    :class:`Message`. Reasoning entries are collected into the message
    so downstream renderers (e.g. the web UI) can display them.
    """
    out: list[Message] = []
    # Buffer for the in-progress assistant message.
    pending_content: str | None = None
    pending_reasoning: str | None = None
    pending_calls: list[ToolCall] = []

    def flush_assistant() -> None:
        nonlocal pending_content, pending_reasoning, pending_calls
        if pending_content is None and pending_reasoning is None and not pending_calls:
            return
        out.append(
            Message(
                role="assistant",
                content=pending_content,
                reasoning=pending_reasoning,
                tool_calls=pending_calls,
            )
        )
        pending_content = None
        pending_reasoning = None
        pending_calls = []

    for it in entries:
        if isinstance(it, InputEntry):
            flush_assistant()
            out.append(Message(role=it.role, content=it.content))
        elif isinstance(it, ReasoningEntry):
            pending_reasoning = (pending_reasoning or "") + it.content
        elif isinstance(it, AssistantTextEntry):
            pending_content = (pending_content or "") + it.content
        elif isinstance(it, ToolCallEntry):
            pending_calls.append(
                ToolCall(id=it.call_id, name=it.name, arguments=it.arguments)
            )
        elif isinstance(it, ToolResultEntry):
            flush_assistant()
            out.append(Message(role="tool", content=it.output, tool_call_id=it.call_id))
    flush_assistant()
    return out


# ---------------------------------------------------------------------------
# Pair-aware slicing for context compaction
# ---------------------------------------------------------------------------


def safe_window(
    entries: list[TranscriptEntry],
    *,
    head: int = 0,
    tail: int,
) -> list[TranscriptEntry]:
    """Return ``entries[:head] + entries[-tail:]`` adjusted to keep tool pairs intact.

    Used by :class:`~lovia.ContextPolicy` implementations to drop a chunk
    from the middle of a transcript without leaving orphan
    :class:`ToolResultEntry`\\ s whose corresponding :class:`ToolCallEntry`
    was sliced away.

    Providers reject such payloads:

    - OpenAI: tool message refers to unknown tool_call_id
    - Anthropic: missing tool_use block

    If a retained ToolResultEntry references a ToolCallEntry that falls
    inside the dropped middle region, the tail window is expanded leftward
    until the ToolCallEntry is included.

    Multiple expansion passes may be required. Example:

        Call(B)
        Call(A)
        Result(B)
        Result(A)

    If only ``Result(A)`` initially falls inside the tail, expanding to
    include ``Call(A)`` also exposes ``Result(B)``, which then requires a
    second expansion to include ``Call(B)``.

    If a retained ToolResultEntry refers to a call that cannot be found
    anywhere outside the preserved head, the orphan result is dropped
    instead.

    Edge cases:
        tail <= 0
            Return entries[:head].

        head + tail >= len(entries)
            Return the entire transcript unchanged.
    """
    n = len(entries)

    if tail <= 0:
        return list(entries[:head])

    head = max(head, 0)

    if head + tail >= n:
        return list(entries)

    head_entries = list(entries[:head])

    # Tool calls already visible because they are preserved in the head.
    head_call_ids = {
        entry.call_id for entry in head_entries if isinstance(entry, ToolCallEntry)
    }

    cut = n - tail

    # Expanding the tail may reveal additional ToolResultEntry instances
    # whose ToolCallEntry lives even earlier in the transcript. Repeat
    # until no orphan tool results remain.
    while True:
        tail_entries = entries[cut:]

        tail_call_ids = {
            entry.call_id for entry in tail_entries if isinstance(entry, ToolCallEntry)
        }

        orphan_call_ids = {
            entry.call_id
            for entry in tail_entries
            if (
                isinstance(entry, ToolResultEntry)
                and entry.call_id not in tail_call_ids
                and entry.call_id not in head_call_ids
            )
        }

        if not orphan_call_ids:
            break

        new_cut = cut

        # Walk left looking for the missing ToolCallEntry instances.
        for i in range(cut - 1, head - 1, -1):
            entry = entries[i]

            if isinstance(entry, ToolCallEntry) and entry.call_id in orphan_call_ids:
                new_cut = i
                orphan_call_ids.discard(entry.call_id)

                if not orphan_call_ids:
                    break

        if new_cut == cut:
            # Remaining orphan results reference ToolCallEntry instances
            # that cannot be found anywhere reachable. Drop those results
            # to keep the transcript valid for provider replay.
            tail_entries = [
                entry
                for entry in tail_entries
                if not (
                    isinstance(entry, ToolResultEntry)
                    and entry.call_id in orphan_call_ids
                )
            ]
            return head_entries + tail_entries

        cut = new_cut

    # If expansion consumed the gap between head and tail, returning the
    # full transcript is simpler than trying to merge overlapping slices.
    if cut <= head:
        return list(entries)

    return head_entries + list(entries[cut:])


__all__ = [
    "AssistantTextEntry",
    "EntryCompletedDelta",
    "FinishDelta",
    "InputEntry",
    "ModelDelta",
    "ReasoningDelta",
    "ReasoningEntry",
    "TextDelta",
    "ToolCallDelta",
    "ToolCallEntry",
    "ToolResultEntry",
    "TranscriptEntry",
    "assistant_to_entries",
    "messages_to_entries",
    "entries_to_messages",
    "entry_from_dict",
    "entry_to_dict",
    "input_to_entries",
    "safe_window",
]
