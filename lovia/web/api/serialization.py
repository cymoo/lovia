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

from dataclasses import asdict
from typing import Any, cast

from ...messages import Message
from ...session import NOTICE_META_KEY, Segment
from ...transcript import (
    InputEntry,
    ToolResultEntry,
    TranscriptEntry,
    entries_to_messages,
)
from ..schemas import ChatSessionInfo, MessageOut, RunRecordInfo
from ..store import ChatMeta, RunRow


def session_info(meta: ChatMeta) -> ChatSessionInfo:
    """Project a metadata row onto the public session-list shape."""
    return ChatSessionInfo(
        id=meta.id,
        title=meta.title,
        agent=meta.agent,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        pinned=meta.pinned,
    )


def run_record(row: RunRow) -> RunRecordInfo:
    """Project a run record onto the public run-history shape."""
    # The store keeps status/usage as plain SQLite values; the supervisor only
    # ever writes the literal statuses and usage_dict shape the model declares.
    return RunRecordInfo(
        run_id=row.id,
        session_id=row.session_id,
        agent=row.agent,
        source=row.source,
        status=cast(Any, row.status),
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
        usage=cast("dict[str, int] | None", row.usage),
    )


def drop_system_entries(entries: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """Strip ``system`` input entries — they are re-generated per run, so no
    view (session detail, attach snapshot, resume seed) should replay them."""
    return [
        e for e in entries if not (isinstance(e, InputEntry) and e.role == "system")
    ]


def _tool_calls(m: Message) -> list[dict[str, Any]]:
    return [
        {"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls
    ]


def _content(m: Message) -> Any:
    """Content for JSON output.

    A turn carrying non-text parts (images/files) is returned as the full part
    list — serialized to plain dicts (``{"type": "image", ...}``) — so the client
    can render attachment thumbnails; ``Message.text`` would collapse them away.
    Plain-text turns return the flattened string (the common case), falling back
    to the raw content when text is empty.
    """
    content = m.content
    if isinstance(content, list) and any(
        getattr(p, "type", "text") != "text" for p in content
    ):
        return [asdict(p) for p in content]
    return m.text or content


def display_text(m: Message) -> str:
    """Stringified display text for plain-text / markdown export."""
    val = m.text or m.content
    return val if isinstance(val, str) else str(val or "")


def _apply_tool_errors(
    outs: list[MessageOut], entries: list[TranscriptEntry]
) -> list[MessageOut]:
    """Stamp ``is_error`` onto tool-result messages.

    ``entries_to_messages`` flattens :class:`ToolResultEntry` down to a plain
    ``tool`` message and loses the error flag the live SSE stream carries, so
    replayed sessions rendered without the error styling. Re-derive it here.
    """
    errs = {
        e.call_id
        for e in entries
        if isinstance(e, ToolResultEntry) and e.is_error and e.call_id
    }
    if errs:
        for o in outs:
            if o.role == "tool" and o.tool_call_id in errs:
                o.is_error = True
    return outs


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
        message_to_out(m, timestamp=created_at + i * spacing)
        for i, m in enumerate(msgs)
    ]


def segments_to_out(
    segments: list[Segment], *, created_at: float, updated_at: float
) -> list[MessageOut]:
    """Project run ``segments`` to the session-detail message shape, splicing one
    synthetic ``context_compacted`` entry after each run that recorded a
    compaction notice in its ``meta``.

    Real messages keep the same evenly-spread timestamps as the flat ``load``
    path (run boundaries never merge — each run opens with a fresh user turn — so
    per-segment grouping matches whole-transcript grouping). Notices are inserted
    at run boundaries; each borrows the timestamp of the message it follows.
    """
    all_msgs: list[Message] = []
    all_entries: list[TranscriptEntry] = []
    boundaries: list[tuple[int, dict[str, Any]]] = []  # (msg index, notice)
    for seg in segments:
        kept = drop_system_entries(seg.entries)
        all_entries.extend(kept)
        all_msgs.extend(entries_to_messages(kept))
        notice = (seg.meta or {}).get(NOTICE_META_KEY)
        if isinstance(notice, dict):
            boundaries.append((len(all_msgs), notice))
    outs = _apply_tool_errors(
        messages_to_out(all_msgs, created_at=created_at, updated_at=updated_at),
        all_entries,
    )
    # Insert from last to first so earlier boundary indices stay valid.
    for idx, notice in reversed(boundaries):
        ts = outs[idx - 1].timestamp if idx > 0 else created_at
        outs.insert(
            idx,
            MessageOut(
                role="context_compacted",
                content=None,
                compaction=notice,
                timestamp=ts,
            ),
        )
    return outs


def view_messages(
    entries: list[TranscriptEntry], *, created_at: float, updated_at: float
) -> list[MessageOut]:
    """Project a transcript (session history + a run's own entries) to the
    session-detail message shape. Shared by ``GET /api/sessions/{id}`` and the
    live re-attach snapshot so both render byte-identically."""
    kept = drop_system_entries(entries)
    return _apply_tool_errors(
        messages_to_out(
            entries_to_messages(kept),
            created_at=created_at,
            updated_at=updated_at,
        ),
        kept,
    )


def message_to_json_dict(m: Message) -> dict[str, Any]:
    """One message in the JSON-export envelope.

    ``tool_call_id``/``name`` are included so a consumer can attribute a tool
    *result* message back to the call it answers (results don't carry the tool
    name themselves).
    """
    return {
        "role": m.role,
        "content": _content(m),
        "reasoning": m.reasoning,
        "tool_calls": _tool_calls(m),
        "tool_call_id": m.tool_call_id,
        "name": m.name,
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
    # A tool *result* message has no name of its own; map call id → name so it
    # can be labelled with the tool it came from. Skip empty ids: some providers
    # default a missing tool-call id to "", which would collide and mislabel
    # results that also have no id.
    tool_names = {tc.id: tc.name for m in msgs for tc in m.tool_calls if tc.id}
    for m in msgs:
        text = display_text(m)
        if m.role == "tool":
            if not text.strip():
                continue
            name = (
                tool_names.get(m.tool_call_id) if m.tool_call_id else None
            ) or m.name
            label = f"Tool result: `{name}`" if name else "Tool result"
            lines.append(f"**{label}**\n\n```\n{text}\n```\n")
            continue
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
