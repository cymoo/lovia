"""Introspection: read-only tools for questions about the web app itself.

"Which workspaces do I have?", "what did we settle in that chat last week?",
"which model is this?" — the answers live in the serving app, not in any
workspace. Without these tools a model with a shell goes looking for them in
lovia's own database. The tools read the app the run is served by
(``RUN_SERVER``), so the plugin takes no arguments and does nothing outside
a served run.

They read; they never change anything. Renaming, archiving and settings stay
the page's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from .. import __version__
from ..exceptions import ToolError
from ..parts import text_of
from ..plugins.base import PluginInstance
from ..run_context import RunContext
from ..tools import Tool, tool
from ..transcript import AssistantTextEntry, InputEntry
from .api.workspaces import workspace_list
from .errors import WebError
from .mcp import SettingsMCP
from .run_deps import RUN_SERVER
from .workspaces import WorkspaceRef, own_root

if TYPE_CHECKING:
    from .api.deps import RouterDeps
    from .store import ChatMeta

_INSTRUCTIONS = (
    "## The lovia app\n"
    "You run in the lovia web app. For questions about the app itself — the "
    "user's workspaces, their other chats and what was said in them, this "
    "chat's directory, mode or model, the configured models, MCP servers — "
    "use `list_workspaces`, `list_chats`, `read_chat` and `lovia_status`. "
    "Never read lovia's own database or config files for this."
)

# What read_chat returns at most: per message, and in all.
_MESSAGE_CHARS = 2000
_TOTAL_CHARS = 16000


def _deps() -> RouterDeps:
    deps = RUN_SERVER.get()
    if deps is None:
        raise ToolError("This tool only works in a chat of the lovia web app.")
    return deps


def _when(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _names(deps: RouterDeps) -> dict[str, str]:
    """Each listed directory's display name, by root."""
    try:
        listed = await workspace_list(deps)
    except WebError:
        return {}
    return {w.root: w.name for w in listed.workspaces}


def _where(root: str | None, names: dict[str, str]) -> str:
    if root is None:
        return "no directory"
    name = names.get(root)
    return f"{name} ({root})" if name else root


@tool(
    name="list_workspaces",
    description=(
        "List the user's workspaces — the directories (local or on another "
        "host) their chats work in, as the sidebar shows them — with each "
        "one's chat count and which one this chat works in."
    ),
)
async def list_workspaces(ctx: RunContext[Any]) -> str:
    deps = _deps()
    here = await _chat_root(deps, ctx)
    counts = await deps.store.chat_counts()
    try:
        listed = (await workspace_list(deps)).workspaces
    except WebError:
        # No per-chat directories here: every chat works in the agent's own.
        own = own_root(ctx.agent)
        if own is None:
            return "This app gives chats no workspace."
        return f"Every chat works in one directory: {own.as_posix()}."
    lines = []
    for w in listed:
        count = counts.get(w.root, 0)
        facts = [f"{count} chat{'' if count == 1 else 's'}"]
        if w.kind == "default":
            facts.insert(0, "where new chats start")
        elif w.kind == "general":
            facts.insert(0, "for chats of no project")
        if w.host:
            facts.append(f"on {w.host}")
        if w.last_used_at:
            facts.append(f"last used {_when(w.last_used_at)}")
        if w.root == here:
            facts.append("this chat")
        lines.append(f"- {w.name} — {w.root} ({'; '.join(facts)})")
    return "\n".join(lines)


async def _chat_root(deps: RouterDeps, ctx: RunContext[Any]) -> str | None:
    binding = deps.run_snapshot(ctx.session_id)
    if binding is not None:
        return binding.root
    meta = await deps.store.get(ctx.session_id) if ctx.session_id else None
    return meta.workspace_root if meta is not None else None


@tool(
    name="list_chats",
    description=(
        "List the user's chats, most recent first (pinned ones lead): id, "
        "title, workspace and last activity. Filter by text in the title, "
        "by workspace, or look among the archived chats. Use an id with "
        "read_chat to see what was said."
    ),
)
async def list_chats(
    ctx: RunContext[Any],
    query: Annotated[str, "Text the title contains; empty lists every chat."] = "",
    workspace: Annotated[
        str | None,
        "Only chats of this workspace: its root or name, as list_workspaces shows it.",
    ] = None,
    archived: Annotated[bool, "List the archived chats instead."] = False,
    limit: Annotated[int, "How many to list, at most 50."] = 20,
) -> str:
    deps = _deps()
    names = await _names(deps)
    root = None
    if workspace is not None:
        root = _resolve_workspace(workspace.strip(), names)
    rows = await deps.store.search(
        query.strip(),
        limit=max(1, min(limit, 50)),
        workspace=root,
        archived=archived,
    )
    if not rows:
        return "No chats match."
    return "\n".join(_chat_line(m, names, ctx.session_id) for m in rows)


def _resolve_workspace(given: str, names: dict[str, str]) -> str:
    if given in names:
        return given
    matches = [root for root, name in names.items() if name == given]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ToolError(
            f"Several workspaces are named {given!r}.",
            hint="pass the root instead: " + ", ".join(matches),
        )
    # An unlisted root (a chat of a directory since dropped from the list).
    return given


def _chat_line(meta: ChatMeta, names: dict[str, str], current: str | None) -> str:
    facts = [_where(meta.workspace_root, names), f"updated {_when(meta.updated_at)}"]
    if meta.pinned:
        facts.append("pinned")
    if meta.archived:
        facts.append("archived")
    if meta.id == current:
        facts.append("this chat")
    return f"- {meta.id}: {meta.title or '(untitled)'} ({'; '.join(facts)})"


@tool(
    name="read_chat",
    description=(
        "Read what was said in another chat: its messages — the user's and "
        "yours, text only, no tool calls or results — newest last. Long chats "
        "come in pages: `skip` the newest messages to read further back."
    ),
)
async def read_chat(
    ctx: RunContext[Any],
    chat_id: Annotated[str, "The chat's id, as list_chats shows it."],
    last: Annotated[int, "How many messages to read, at most 50."] = 20,
    skip: Annotated[int, "How many of the newest messages to skip."] = 0,
) -> str:
    deps = _deps()
    meta = await deps.store.get(chat_id.strip())
    if meta is None:
        raise ToolError(
            f"No chat {chat_id!r}.", hint="list_chats shows the ids of chats"
        )
    messages = _messages(await deps.session.load(meta.id))
    end = max(0, len(messages) - max(0, skip))
    page = messages[max(0, end - max(1, min(last, 50))) : end]
    # Newest first until the budget runs out, so a page never ends mid-way.
    kept: list[str] = []
    budget = _TOTAL_CHARS
    for role, text in reversed(page):
        block = f"[{role}] {_clip(text, _MESSAGE_CHARS)}"
        if kept and len(block) > budget:
            break
        kept.append(block)
        budget -= len(block)
    kept.reverse()
    first = end - len(kept) + 1
    names = await _names(deps)
    head = (
        f"Chat {meta.id}: {meta.title or '(untitled)'} — "
        f"{_where(meta.workspace_root, names)}. "
    )
    if not kept:
        return head + (
            "It has no messages yet." if not messages else "No messages that far back."
        )
    head += f"Messages {first}–{end} of {len(messages)}."
    if deps.run_snapshot(meta.id) is not None and meta.id != ctx.session_id:
        head += " A reply is in progress there; it isn't included yet."
    return head + "\n\n" + "\n\n".join(kept)


def _messages(entries: list[Any]) -> list[tuple[str, str]]:
    """The chat as the user saw it: their messages and the assistant's text,
    a run of assistant text between tool calls joined into one."""
    out: list[tuple[str, str]] = []
    for entry in entries:
        if isinstance(entry, InputEntry) and entry.role == "user":
            text, role = text_of(entry.content), "user"
        elif isinstance(entry, AssistantTextEntry):
            text, role = entry.content, "assistant"
        else:
            continue
        text = text.strip()
        if not text:
            continue
        if out and out[-1][0] == role == "assistant":
            out[-1] = (role, f"{out[-1][1]}\n\n{text}")
        else:
            out.append((role, text))
    return out


@tool(
    name="lovia_status",
    description=(
        "This chat and the app it runs in: the lovia version, this chat's "
        "workspace, permission mode and model, the configured models, and the "
        "MCP servers with how each one's last connection went."
    ),
)
async def lovia_status(ctx: RunContext[Any]) -> str:
    deps = _deps()
    meta = await deps.store.get(ctx.session_id) if ctx.session_id else None
    lines = [f"lovia {__version__}"]
    lines += _chat_lines(deps, ctx, meta, await _names(deps))
    lines += _model_lines(deps, meta)
    lines += _mcp_lines(deps, ctx)
    return "\n".join(lines)


def _chat_lines(
    deps: RouterDeps, ctx: RunContext[Any], meta: ChatMeta | None, names: dict[str, str]
) -> list[str]:
    title = (meta.title if meta else None) or "(untitled)"
    lines = [f"This chat: {ctx.session_id} — {title}"]
    binding = deps.run_snapshot(ctx.session_id)
    root = binding.root if binding else meta.workspace_root if meta else None
    if root is not None:
        ref = WorkspaceRef.parse(root)
        where = _where(root, names)
        if ref.remote and ref.host_name:
            where += f", on {ref.host_name}"
        lines.append(f"- workspace: {where}")
        mode = binding.mode if binding else meta.workspace_mode if meta else None
        lines.append("- mode: " + (mode or "the agent's own policy"))
    provider = ctx.agent.model
    model = getattr(provider, "model", None) or "unknown"
    lines.append(f"- model of this reply: {model} ({getattr(provider, 'name', '?')})")
    return lines


def _model_lines(deps: RouterDeps, meta: ChatMeta | None) -> list[str]:
    runtime = deps.config_runtime
    if runtime is None:
        return []
    config = runtime.config
    default = config.roles.chat
    picked = meta.model if meta is not None else None
    lines = [
        "- model setting: "
        + (picked if picked and config.profile(picked) else f"default ({default})")
        + " — a change applies from the next message",
        "Configured models:",
    ]
    for p in config.models:
        facts = [p.model]
        if p.id == default:
            facts.append("default")
        lines.append(f"- {p.id}: {p.display_name} ({', '.join(facts)})")
    return lines


def _mcp_lines(deps: RouterDeps, ctx: RunContext[Any]) -> list[str]:
    plugin = next((p for p in ctx.agent.plugins if isinstance(p, SettingsMCP)), None)
    if plugin is None:
        return []
    # A project's own server stands in for a global one of its name.
    on = {s.name: s for s in plugin.servers_for(ctx)}
    servers = {**{s.name: s for s in plugin.servers}, **on}
    if not servers:
        return ["MCP servers: none configured"]
    ctrl = deps.supervisor.get(ctx.session_id) if ctx.session_id else None
    started = ctrl.started_at if ctrl is not None else None
    lines = ["MCP servers:"]
    for name, cfg in sorted(servers.items()):
        if name not in on:
            lines.append(f"- {name}: off for this chat")
            continue
        status = plugin.book.get(cfg)
        if status is None:
            seen = "not connected yet"
        else:
            this_run = started is not None and status.at >= started
            when = "for this run" if this_run else f"last at {_when(status.at)}"
            seen = (
                f"connected {when}, {len(status.tools)} tools"
                if status.ok
                else f"failed to connect {when}"
            )
        lines.append(f"- {name}: on — {seen}")
    return lines


@dataclass
class Introspection:
    """Plugin: read-only tools for questions about the web app itself —
    ``list_workspaces``, ``list_chats``, ``read_chat``, ``lovia_status``.

    ``lovia web`` serves its assistant with it; an app of your own adds it
    with ``plugins=[Introspection()]``. The tools see every chat the app's
    store holds, so add it only where those chats all belong to one person.
    """

    instructions: str | None = _INSTRUCTIONS
    name: str = "introspection"

    async def setup(self, ctx: RunContext[Any]) -> PluginInstance:
        tools: list[Tool] = [list_workspaces, list_chats, read_chat, lovia_status]
        return PluginInstance(tools=tools, instructions=self.instructions)


__all__ = ["Introspection"]
