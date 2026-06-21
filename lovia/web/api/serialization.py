"""Framework-free serialization shared by the API routers.

Kept import-light on purpose — NO ``fastapi`` here, only lovia core + the web
schemas — so the same helpers serve both Pydantic responses (``SessionDetail``)
and the plain-dict / plain-text exports.

The live-streaming formatters in :mod:`lovia.web.sse` are deliberately *not*
shared with this module: they render a different shape for the SSE UI (e.g.
pydantic tool results as ``key: value`` lines), and folding them together would
change the wire format.
"""

from __future__ import annotations

from typing import Any

from ...messages import Message
from ..schemas import ChatSessionInfo, MessageOut
from ..store import ChatMeta


def session_info(meta: ChatMeta) -> ChatSessionInfo:
    """Project a metadata row onto the public session-list shape."""
    return ChatSessionInfo(
        id=meta.id,
        title=meta.title,
        agent=meta.agent,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
    )


def _tool_calls(m: Message) -> list[dict[str, Any]]:
    return [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls]


def _content(m: Message) -> Any:
    """Content for JSON output: the flattened text, else the raw content.

    ``Message.text`` collapses multimodal parts to a string; when it is empty we
    fall back to the original ``content`` (a part list or ``None``) so multimodal
    turns aren't silently dropped.
    """
    return m.text or m.content


def display_text(m: Message) -> str:
    """Stringified display text for plain-text / markdown export."""
    val = m.text or m.content
    return val if isinstance(val, str) else str(val or "")


def message_to_out(m: Message, *, timestamp: float | None = None) -> MessageOut:
    return MessageOut(
        role=m.role,
        content=_content(m),
        reasoning=m.reasoning,
        tool_call_id=m.tool_call_id,
        name=m.name,
        tool_calls=_tool_calls(m),
        timestamp=timestamp,
    )


def messages_to_out(
    msgs: list[Message], *, created_at: float, updated_at: float
) -> list[MessageOut]:
    """Convert messages to ``MessageOut``, spreading synthetic per-message
    timestamps evenly across the session's ``[created_at, updated_at]`` span."""
    n = len(msgs)
    spacing = 0.0 if n <= 1 else max(0.0, updated_at - created_at) / (n - 1)
    return [
        message_to_out(m, timestamp=created_at + i * spacing) for i, m in enumerate(msgs)
    ]


def message_to_json_dict(m: Message) -> dict[str, Any]:
    """One message in the JSON-export envelope."""
    return {
        "role": m.role,
        "content": _content(m),
        "reasoning": m.reasoning,
        "tool_calls": _tool_calls(m),
    }


def export_txt(msgs: list[Message]) -> str:
    """Render a transcript as plain text."""
    lines: list[str] = []
    for m in msgs:
        text = display_text(m)
        if text:
            lines.append(f"## {m.role.upper()}\n\n{text}\n")
        for tc in m.tool_calls:
            lines.append(f"### Tool: {tc.name}\n```\n{tc.arguments}\n```\n")
    return "\n".join(lines)


def export_md(msgs: list[Message], *, title: str, session_id: str) -> str:
    """Render a transcript as Markdown.

    Reasoning is a *visible* blockquote (not a collapsed ``<details>``) so it
    survives a Markdown→PDF conversion, and it precedes the answer under one
    heading — the model reasons first, so the export mirrors that order.
    """
    lines: list[str] = [f"# {title}\n", f"*Session: `{session_id}`*\n"]
    for m in msgs:
        text = display_text(m)
        if text or m.reasoning or m.tool_calls:
            lines.append(f"### {m.role.capitalize()}\n")
        if m.reasoning:
            quoted = "\n".join(
                f"> {ln}" if ln.strip() else ">" for ln in m.reasoning.splitlines()
            )
            lines.append(f"> **💭 Thinking**\n>\n{quoted}\n")
        if text:
            lines.append(f"{text}\n")
        for tc in m.tool_calls:
            lines.append(f"**Tool: `{tc.name}`**\n\n```json\n{tc.arguments}\n```\n")
    return "\n".join(lines)
