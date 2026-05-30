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

from dataclasses import asdict, dataclass
from typing import Any, Literal, Union

from .content import ContentBlock, ImageBlock, TextBlock
from .messages import AssistantMessage, ChatMessage, ToolCall, Usage


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


Item = Union[
    InputMessageItem,
    MessageOutputItem,
    ReasoningItem,
    ToolCallItem,
    ToolCallOutputItem,
]
"""Discriminated union of every item kind that can appear in a transcript.

Handoffs reuse :class:`ToolCallItem` / :class:`ToolCallOutputItem`; we
add specialised types only when a provider exposes a structurally
distinct concept that we cannot lossily flatten (e.g. server-side
tools on OpenAI Responses — coming in a future release).
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
}


def item_to_dict(item: Item) -> dict[str, Any]:
    """Serialize an Item to a JSON-safe ``dict``.

    Handles :class:`InputMessageItem` specially because ``ContentBlock``
    dataclasses also need their ``type`` discriminator preserved.
    :class:`ToolCallOutputItem` is handled specially because its ``raw``
    field may hold arbitrary Python objects (e.g. Pydantic models) that are
    not JSON-serializable; ``raw`` is never restored on deserialization so it
    is always omitted here.
    ``dataclasses.asdict`` does the right thing for everything else.
    """
    if isinstance(item, InputMessageItem):
        return {
            "type": item.type,
            "role": item.role,
            "content": _content_to_dict(item.content),
        }
    if isinstance(item, ToolCallOutputItem):
        return {
            "type": item.type,
            "call_id": item.call_id,
            "output": item.output,
            "is_error": item.is_error,
            "raw": None,
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


# ---------------------------------------------------------------------------
# ChatMessage ↔ Item conversions (Phase 9b boundary)
#
# These helpers let the runner pivot to Items internally while providers and
# the persisted transcript continue to speak ChatMessage. They are also what
# 9c / 9d will repurpose when the wire boundary moves: the conversion logic
# below becomes the inverse of what each provider adapter ends up doing.
# ---------------------------------------------------------------------------


def assistant_to_items(am: AssistantMessage) -> list[Item]:
    """Split an :class:`AssistantMessage` into its component Items.

    Order matches the conceptual emission order: reasoning first (it logically
    precedes the visible answer), then the message body, then any tool calls.
    Empty / absent fields are skipped — a tool-only turn produces just the
    ``ToolCallItem``\\ s with no preceding message item.
    """
    out: list[Item] = []
    if am.reasoning_content:
        out.append(ReasoningItem(content=am.reasoning_content))
    if am.content:
        out.append(MessageOutputItem(content=am.content))
    for tc in am.tool_calls:
        out.append(ToolCallItem(call_id=tc.id, name=tc.name, arguments=tc.arguments))
    return out


def input_to_items(messages: list[ChatMessage]) -> list[Item]:
    """Translate a system/user message prefix to :class:`InputMessageItem`\\ s.

    Only ``system`` and ``user`` roles are accepted — the runner uses this to
    seed ``items_log`` with the initial transcript. Assistant / tool messages
    in a snapshot are handled by :func:`transcript_to_items` instead.
    """
    out: list[Item] = []
    for m in messages:
        if m.role not in ("system", "user"):
            raise ValueError(f"input_to_items: unexpected role {m.role!r}")
        content = m.content if m.content is not None else ""
        out.append(InputMessageItem(role=m.role, content=content))  # type: ignore[arg-type]
    return out


def transcript_to_items(messages: list[ChatMessage]) -> list[Item]:
    """Translate a full transcript (any roles) back to Items.

    Used when resuming from a snapshot. ``tool`` messages have no ``raw``
    return value to recover, so ``ToolCallOutputItem.raw`` is left ``None``.
    """
    out: list[Item] = []
    for m in messages:
        if m.role in ("system", "user"):
            content = m.content if m.content is not None else ""
            out.append(InputMessageItem(role=m.role, content=content))  # type: ignore[arg-type]
        elif m.role == "assistant":
            if m.reasoning_content:
                out.append(ReasoningItem(content=m.reasoning_content))
            if m.content:
                # ``content`` may be a list[ContentBlock]; flatten to text for
                # the MessageOutputItem (richer shapes are 9d territory).
                from .content import text_of

                out.append(MessageOutputItem(content=text_of(m.content)))
            for tc in m.tool_calls:
                out.append(
                    ToolCallItem(call_id=tc.id, name=tc.name, arguments=tc.arguments)
                )
        elif m.role == "tool":
            from .content import text_of

            out.append(
                ToolCallOutputItem(
                    call_id=m.tool_call_id or "",
                    output=text_of(m.content),
                )
            )
        else:  # pragma: no cover - defensive
            raise ValueError(f"transcript_to_items: unknown role {m.role!r}")
    return out


def items_to_chat_messages(items: list[Item]) -> list[ChatMessage]:
    """Inverse of :func:`transcript_to_items`.

    Groups consecutive assistant-side items (reasoning + message + tool calls)
    into one :class:`ChatMessage`, matching how the runner appends them.
    """
    out: list[ChatMessage] = []
    # Buffer for the in-progress assistant message.
    pending_reasoning: str | None = None
    pending_content: str | None = None
    pending_calls: list[ToolCall] = []

    def flush_assistant() -> None:
        nonlocal pending_reasoning, pending_content, pending_calls
        if pending_content is None and not pending_calls and pending_reasoning is None:
            return
        out.append(
            ChatMessage(
                role="assistant",
                content=pending_content,
                tool_calls=pending_calls,
                reasoning_content=pending_reasoning,
            )
        )
        pending_reasoning = None
        pending_content = None
        pending_calls = []

    for it in items:
        if isinstance(it, InputMessageItem):
            flush_assistant()
            out.append(ChatMessage(role=it.role, content=it.content))
        elif isinstance(it, ReasoningItem):
            pending_reasoning = it.content
        elif isinstance(it, MessageOutputItem):
            pending_content = it.content
        elif isinstance(it, ToolCallItem):
            pending_calls.append(
                ToolCall(id=it.call_id, name=it.name, arguments=it.arguments)
            )
        elif isinstance(it, ToolCallOutputItem):
            flush_assistant()
            out.append(
                ChatMessage(role="tool", content=it.output, tool_call_id=it.call_id)
            )
    flush_assistant()
    return out


# ---------------------------------------------------------------------------
# Pair-aware slicing for context compaction
# ---------------------------------------------------------------------------


def safe_window(
    items: list[Item],
    *,
    head: int = 0,
    tail: int,
) -> list[Item]:
    """Return ``items[:head] + items[-tail:]`` adjusted to keep tool pairs intact.

    Used by :class:`~lovia.ContextPolicy` implementations to drop a chunk
    from the middle of a transcript without leaving orphan
    :class:`ToolCallOutputItem`\\ s whose corresponding :class:`ToolCallItem`
    was sliced away — providers reject such payloads (OpenAI: "tool message
    refers to unknown tool_call_id"; Anthropic: missing ``tool_use``).

    If a kept output's call lives inside the dropped middle, the cut is
    walked backward until the call is included (i.e. the tail grows). If
    the matching call cannot be found anywhere, the orphan output is
    dropped instead. ``head`` items are always preserved as-is.

    Edge cases:
    * ``tail <= 0``         → returns ``items[:head]`` (drop everything else)
    * ``head + tail >= n``  → returns ``list(items)`` (nothing to drop)
    """
    n = len(items)
    if tail <= 0:
        return list(items[:head])
    if head < 0:
        head = 0
    if head + tail >= n:
        return list(items)

    head_items = list(items[:head])
    head_call_ids: set[str] = {
        it.call_id for it in head_items if isinstance(it, ToolCallItem)
    }

    cut = n - tail
    # Iterate to a fixed point: each expansion of `cut` may pull in more
    # ToolCallOutputItems whose calls are still earlier. In practice this
    # converges in 1–2 passes because tool calls don't nest.
    for _ in range(n):
        tail_slice = items[cut:]
        tail_call_ids = {
            it.call_id for it in tail_slice if isinstance(it, ToolCallItem)
        }
        orphans = {
            it.call_id
            for it in tail_slice
            if isinstance(it, ToolCallOutputItem)
            and it.call_id not in tail_call_ids
            and it.call_id not in head_call_ids
        }
        if not orphans:
            break
        new_cut = cut
        for i in range(cut - 1, head - 1, -1):
            it = items[i]
            if isinstance(it, ToolCallItem) and it.call_id in orphans:
                new_cut = i
                orphans.discard(it.call_id)
                if not orphans:
                    break
        if new_cut == cut:
            # Remaining orphans have no matching call anywhere reachable;
            # drop those outputs to keep the payload valid.
            tail_slice = [
                it
                for it in tail_slice
                if not (isinstance(it, ToolCallOutputItem) and it.call_id in orphans)
            ]
            return head_items + tail_slice
        cut = new_cut

    # If the expansion swallowed the head boundary, fall back to the
    # whole transcript rather than emit duplicates.
    if cut <= head:
        return list(items)
    return head_items + list(items[cut:])
