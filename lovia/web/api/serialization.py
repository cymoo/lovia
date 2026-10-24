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
from typing import Any, NamedTuple, cast

from ...messages import Message
from ...parts import ImagePart
from ...session import NOTICE_META_KEY, TIMING_META_KEY, Segment
from ...transcript import (
    InputEntry,
    ToolResultEntry,
    TranscriptEntry,
    entries_to_messages,
)
from ..schemas import ChatSessionInfo, MessageOut, RunRecordInfo, ToolImageOut
from ..store import ChatMeta, RunRow


def session_info(
    meta: ChatMeta, *, last_run_status: str | None = None
) -> ChatSessionInfo:
    """Project a metadata row onto the public session-list shape."""
    return ChatSessionInfo(
        id=meta.id,
        title=meta.title,
        agent=meta.agent,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        pinned=meta.pinned,
        parent_id=meta.parent_id,
        last_run_status=last_run_status,
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


def tool_image_stubs(parts: list[Any] | None) -> list[ToolImageOut]:
    """Image stubs for one tool result's parts (0-based over its image parts)."""
    if not parts:
        return []
    return [
        ToolImageOut(index=i, mime_type=p.mime_type)
        for i, p in enumerate(p for p in parts if isinstance(p, ImagePart))
    ]


def tool_result_flags(
    entries: list[TranscriptEntry],
) -> tuple[set[str], dict[str, list[ToolImageOut]]]:
    """Per call id, what ``entries_to_messages`` drops when it flattens a
    :class:`ToolResultEntry` to a plain ``tool`` message: the ids whose call
    failed, and each result's image stubs (no bytes — the client fetches them
    from the tool-images route). Replays and exports re-derive them here so
    they match what the live SSE stream carried."""
    errors: set[str] = set()
    images: dict[str, list[ToolImageOut]] = {}
    for e in entries:
        if isinstance(e, ToolResultEntry) and e.call_id:
            if e.is_error:
                errors.add(e.call_id)
            if stubs := tool_image_stubs(e.parts):
                images[e.call_id] = stubs
    return errors, images


def _apply_tool_flags(
    outs: list[MessageOut], entries: list[TranscriptEntry]
) -> list[MessageOut]:
    """Stamp ``is_error`` and image stubs onto tool-result messages."""
    errors, images = tool_result_flags(entries)
    for o in outs:
        if o.role == "tool" and o.tool_call_id:
            o.is_error = o.is_error or o.tool_call_id in errors
            if o.tool_call_id in images:
                o.images = images[o.tool_call_id]
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


class Run(NamedTuple):
    """One run a view covers. ``run_id`` is None while it is in flight (its
    turns get no ref yet); ``timing`` is its segment's recorded ``(started_at,
    finished_at)``, None when unrecorded."""

    run_id: str | None
    entries: list[TranscriptEntry]
    timing: tuple[float, float] | None = None


#: The runs a view covers, in transcript order.
Runs = list[Run]


def _fill_gaps(times: list[float | None], first: float, last: float) -> list[float]:
    """``times`` with each stretch of unknowns spread evenly between its known
    neighbours, exclusive — or ``first``/``last``, inclusive, at either end,
    though never past a known neighbour: the result stays in order."""
    out = list(times)
    i, n = 0, len(out)
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        lo = out[i - 1] if i > 0 else None
        hi = out[j] if j < n else None
        a = first if lo is None else lo
        if hi is not None:
            a = min(a, hi)
        b = max(a, last if hi is None else hi)
        steps = (j - i - 1) + (lo is not None) + (hi is not None)
        for k in range(j - i):
            pos = k + (lo is not None)
            out[i + k] = a + (b - a) * pos / steps if steps else a
        i = j
    return cast(list[float], out)


def _runs_to_out(
    runs: Runs, *, created_at: float, updated_at: float
) -> tuple[list[MessageOut], list[int]]:
    """Each run's messages, and the index where each run's messages end.

    A timed run's messages span its start (its opening user turn) to its finish
    (its last answer, which carries its ``duration``), evenly between. An untimed run's — written before timing was, or still in
    flight — spread between their known neighbours, the session's
    ``created_at``/``updated_at`` standing in at the ends. Runs never merge
    (each opens with a fresh user turn), so projecting run by run matches
    projecting the whole transcript.
    """
    outs: list[MessageOut] = []
    entries: list[TranscriptEntry] = []
    times: list[float | None] = []
    ends: list[int] = []
    for run in runs:
        kept = drop_system_entries(run.entries)
        entries.extend(kept)
        msgs = [message_to_out(m) for m in entries_to_messages(kept)]
        if run.timing is not None and msgs:
            start, finish = run.timing
            # The answer is where the finish shows: anything after it (a
            # stopped run's trailing tool result) finished then too.
            end = next(
                (
                    i
                    for i in range(len(msgs) - 1, -1, -1)
                    if msgs[i].role == "assistant"
                ),
                len(msgs) - 1,
            )
            span = max(end, 1)
            times.extend(
                start + (finish - start) * min(i, end) / span for i in range(len(msgs))
            )
            if msgs[end].role == "assistant":
                msgs[end].duration = max(0.0, finish - start)
        else:
            times.extend([None] * len(msgs))
        outs.extend(msgs)
        ends.append(len(outs))
    for out, ts in zip(outs, _fill_gaps(times, created_at, updated_at)):
        out.timestamp = ts
    return _apply_tool_flags(outs, entries), ends


def user_turn_refs(runs: Runs) -> list[str | None]:
    """Each user turn's ref, in transcript order: ``"<run_id>:<n>"`` — its
    run's segment id and its place among that run's user turns.

    A run's segment is never appended to again (a rewind only cuts a run's
    tail; new turns always start a new run), so a ref once cut never comes
    back naming another turn — unlike a turn's position, which a rewind and a
    retry reuse.
    """
    refs: list[str | None] = []
    for run in runs:
        n = 0
        for e in run.entries:
            if isinstance(e, InputEntry) and e.role == "user":
                refs.append(f"{run.run_id}:{n}" if run.run_id else None)
                n += 1
    return refs


def stamp_refs(outs: list[MessageOut], runs: Runs) -> list[MessageOut]:
    """Give each user message its ref (see :func:`user_turn_refs`). When the
    counts disagree nothing is stamped: no ref beats a wrong one."""
    users = [o for o in outs if o.role == "user"]
    refs = user_turn_refs(runs)
    if len(users) == len(refs):
        for out, ref in zip(users, refs):
            out.ref = ref
    return outs


def segment_timing(meta: dict[str, Any] | None) -> tuple[float, float] | None:
    """A segment's recorded ``(started_at, finished_at)``, or None."""
    timing = (meta or {}).get(TIMING_META_KEY)
    if isinstance(timing, dict):
        start, finish = timing.get("started_at"), timing.get("finished_at")
        if isinstance(start, (int, float)) and isinstance(finish, (int, float)):
            return float(start), float(finish)
    return None


def segments_runs(segments: list[Segment]) -> Runs:
    return [Run(seg.run_id, seg.entries, segment_timing(seg.meta)) for seg in segments]


def segments_to_out(
    segments: list[Segment], *, created_at: float, updated_at: float
) -> list[MessageOut]:
    """Project run ``segments`` to the session-detail message shape (see
    :func:`_runs_to_out`), splicing one synthetic ``context_compacted`` entry
    after each run that recorded a compaction notice in its ``meta``; each
    borrows the timestamp of the message it follows."""
    runs = segments_runs(segments)
    outs, ends = _runs_to_out(runs, created_at=created_at, updated_at=updated_at)
    # Insert from last to first so earlier boundary indices stay valid.
    for seg, end in reversed(list(zip(segments, ends))):
        notice = (seg.meta or {}).get(NOTICE_META_KEY)
        if isinstance(notice, dict):
            outs.insert(
                end,
                MessageOut(
                    role="context_compacted",
                    content=None,
                    compaction=notice,
                    timestamp=outs[end - 1].timestamp if end > 0 else created_at,
                ),
            )
    return stamp_refs(outs, runs)


def view_messages(
    runs: Runs, *, created_at: float, updated_at: float
) -> list[MessageOut]:
    """Project session history + a run in flight to the session-detail message
    shape (see :func:`_runs_to_out`), refs stamped. Shared by ``GET
    /api/sessions/{id}`` and the live re-attach snapshot so both render
    byte-identically."""
    outs, _ = _runs_to_out(runs, created_at=created_at, updated_at=updated_at)
    return stamp_refs(outs, runs)


def message_to_json_dict(
    m: Message,
    *,
    errors: set[str] | None = None,
    images: dict[str, list[ToolImageOut]] | None = None,
) -> dict[str, Any]:
    """One message in the JSON-export envelope.

    ``tool_call_id``/``name`` are included so a consumer can attribute a tool
    *result* message back to the call it answers (results don't carry the tool
    name themselves). A result also carries ``is_error`` and its ``images``
    (from :func:`tool_result_flags`), as the session view does.
    """
    out: dict[str, Any] = {
        "role": m.role,
        "content": _content(m),
        "reasoning": m.reasoning,
        "tool_calls": _tool_calls(m),
        "tool_call_id": m.tool_call_id,
        "name": m.name,
    }
    if m.role == "tool" and m.tool_call_id:
        out["is_error"] = m.tool_call_id in (errors or ())
        out["images"] = [i.model_dump() for i in (images or {}).get(m.tool_call_id, [])]
    return out


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
