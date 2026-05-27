"""Run items: a discriminated union of everything that can appear in a transcript.

`Item` is what lovia uses internally to represent the transcript and what the
public API surfaces in ``RunResult.new_items``, hooks, and events. Each Item is
a small typed record; provider adapters translate between Items and their wire
format (OpenAI Chat ``messages``, OpenAI Responses ``input``/``output``,
Anthropic Messages, …) so the runner core stays provider-agnostic.

Items are deliberately implemented as plain ``dataclass``\\ es rather than
Pydantic models — they sit on the hot path (one per assistant token batch, one
per tool call) and we want minimal overhead and easy ``match`` ergonomics.
``item_to_dict`` / ``item_from_dict`` handle (de)serialization for sessions and
checkpoints; the ``type`` field acts as the discriminator.

Streaming providers don't emit whole Items per chunk — they emit
:class:`ItemDelta` values (``TextDelta`` / ``ReasoningDelta`` /
``ToolCallDelta``) that the runner assembles into Items at end-of-turn.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Union

from .content import ContentBlock, ImageBlock, TextBlock
from .messages import Usage


# ---------------------------------------------------------------------------
# Conversation items
# ---------------------------------------------------------------------------


@dataclass
class InputMessageItem:
    """A ``system`` or ``user`` message contributed by the caller.

    Shape is identical for both roles, so we use a single class with a
    ``role`` field instead of two near-duplicates. ``content`` follows the
    same convention as :class:`ChatMessage`: a plain string for the common
    case, or a list of typed :class:`ContentBlock`\\ s for multimodal input.
    """

    role: Literal["system", "user"]
    content: str | list[ContentBlock]
    type: Literal["input_message"] = "input_message"


@dataclass
class MessageOutputItem:
    """A textual assistant response.

    ``id`` is set when the provider assigns a stable identifier (OpenAI
    Responses API returns one per output item); it is ``None`` for Chat
    Completions and most other providers.
    """

    content: str
    id: str | None = None
    type: Literal["message_output"] = "message_output"


@dataclass
class ReasoningItem:
    """Chain-of-thought output that the model wants echoed in subsequent turns.

    ``content`` may be plain text (DeepSeek thinking, Anthropic extended
    thinking) or an opaque encrypted blob (OpenAI o-series via Responses).
    Either way the runner must preserve it verbatim and replay it back to
    the provider; treating it as opaque keeps providers honest.
    """

    content: str
    id: str | None = None
    type: Literal["reasoning"] = "reasoning"


@dataclass
class ToolCallItem:
    """A function-tool call the model wants to invoke.

    ``arguments`` is the raw JSON string as emitted by the model — we keep
    it unparsed so error messages can quote the exact payload.
    """

    call_id: str
    name: str
    arguments: str
    type: Literal["tool_call"] = "tool_call"


@dataclass
class ToolCallOutputItem:
    """The (already-rendered) result of executing a :class:`ToolCallItem`.

    ``output`` is the string the model will see. ``raw`` preserves the
    Python-side return value when the tool returned something structured
    so hooks and renderers can inspect it later without re-parsing.
    """

    call_id: str
    output: str
    raw: Any = None
    is_error: bool = False
    type: Literal["tool_call_output"] = "tool_call_output"


@dataclass
class HandoffCallItem:
    """A tool call that the runner recognised as a handoff trigger.

    Carries the same fields as :class:`ToolCallItem` plus the resolved
    target agent name so consumers don't have to re-derive it.
    """

    call_id: str
    name: str
    arguments: str
    target_agent: str
    type: Literal["handoff_call"] = "handoff_call"


@dataclass
class HandoffOutputItem:
    """Marker emitted after a handoff completes.

    ``source_agent`` is the agent that initiated the handoff;
    ``target_agent`` is the one taking over. ``message`` is an optional
    human-readable note (rendered as the tool result the source agent
    sees).
    """

    call_id: str
    source_agent: str
    target_agent: str
    message: str = ""
    type: Literal["handoff_output"] = "handoff_output"


@dataclass
class ServerToolCallItem:
    """A provider-side built-in tool invocation (web_search, file_search, …).

    We don't try to normalise these across providers — the shape is too
    vendor-specific. ``data`` holds the provider's raw payload so it can
    be round-tripped back to the same provider next turn.
    """

    provider: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    id: str | None = None
    type: Literal["server_tool_call"] = "server_tool_call"


Item = Union[
    InputMessageItem,
    MessageOutputItem,
    ReasoningItem,
    ToolCallItem,
    ToolCallOutputItem,
    HandoffCallItem,
    HandoffOutputItem,
    ServerToolCallItem,
]
"""Discriminated union of every item kind that can appear in a transcript."""


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
    which parallel call the fragment belongs to (OpenAI Chat indexes them;
    Responses gives stable IDs). ``call_id`` / ``name`` are typically set on
    the first chunk; subsequent chunks only carry ``arguments``.
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


ItemDelta = Union[TextDelta, ReasoningDelta, ToolCallDelta, UsageDelta, FinishDelta]
"""Discriminated union of streaming deltas a provider may yield."""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


_ITEM_TYPES: dict[str, type[Any]] = {
    "input_message": InputMessageItem,
    "message_output": MessageOutputItem,
    "reasoning": ReasoningItem,
    "tool_call": ToolCallItem,
    "tool_call_output": ToolCallOutputItem,
    "handoff_call": HandoffCallItem,
    "handoff_output": HandoffOutputItem,
    "server_tool_call": ServerToolCallItem,
}


def item_to_dict(item: Item) -> dict[str, Any]:
    """Serialize an Item to a JSON-safe ``dict``.

    Handles :class:`InputMessageItem` specially because ``ContentBlock``
    dataclasses also need their ``type`` discriminator preserved.
    ``dataclasses.asdict`` does the right thing for everything else.
    """
    if isinstance(item, InputMessageItem):
        return {
            "type": item.type,
            "role": item.role,
            "content": _content_to_dict(item.content),
        }
    return asdict(item)


def item_from_dict(data: dict[str, Any]) -> Item:
    """Deserialize an Item produced by :func:`item_to_dict`.

    Raises ``ValueError`` on unknown discriminator — callers are expected
    to have a stable schema (we don't silently drop unknown fields, since
    that's how reasoning items get lost).
    """
    type_ = data.get("type")
    if type_ not in _ITEM_TYPES:
        raise ValueError(f"Unknown item type: {type_!r}")
    cls = _ITEM_TYPES[type_]
    if cls is InputMessageItem:
        return InputMessageItem(
            role=data["role"],
            content=_content_from_dict(data["content"]),
        )
    # Strip ``type`` since it's the dataclass default; pass everything else
    # through so we surface unknown fields as a clean TypeError.
    payload = {k: v for k, v in data.items() if k != "type"}
    return cls(**payload)  # type: ignore[no-any-return]


def _content_to_dict(
    content: str | list[ContentBlock],
) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    return [asdict(block) for block in content]


def _content_from_dict(
    content: str | list[dict[str, Any]],
) -> str | list[ContentBlock]:
    if isinstance(content, str):
        return content
    blocks: list[ContentBlock] = []
    for raw in content:
        t = raw.get("type")
        if t == "text":
            blocks.append(TextBlock(text=raw["text"]))
        elif t == "image":
            blocks.append(
                ImageBlock(
                    url=raw.get("url"),
                    data=raw.get("data"),
                    mime_type=raw.get("mime_type"),
                    detail=raw.get("detail"),
                )
            )
        else:
            raise ValueError(f"Unknown content block type: {t!r}")
    return blocks
