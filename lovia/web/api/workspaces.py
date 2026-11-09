"""Workspace routes: the directories a chat may work in.

``GET /api/workspaces`` is the page's list — the agent's own directory and
the switch's general one first (always there), then every directory added or
used, most recent first. The list is interface state: what a chat may open
is decided by :meth:`RouterDeps.admit_root` on every run, never by it.
Chats from before chats had a directory are bound to one here too.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, Query
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ..errors import WebError
from ..schemas import (
    AssociateRequest,
    AssociateResponse,
    WorkspaceAdd,
    WorkspaceItem,
    WorkspaceList,
)
from ..workspaces import own_root
from .deps import RouterDeps

# A completion lists this many directories at most, from at most this many
# entries read (a huge directory must not stall the request).
_MAX_SUGGESTIONS = 50
_MAX_SCANNED = 5000


def _suggest(raw: str, roots: tuple[Path, ...] | None) -> list[str]:
    """Subdirectories completing ``raw`` (a path being typed), as typed.

    ``raw`` ending in a separator lists that directory; otherwise its parent,
    filtered by the last segment (case-insensitively). Hidden directories
    show only once a dot is typed. ``roots`` limits the tree: a suggestion
    must lie under one of them or on the way to one.
    """
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        return []
    # Split on the last separator by hand: Path would normalize a trailing
    # "." (a hidden name being typed) away.
    cut = max(expanded.rfind("/"), expanded.rfind(os.sep)) + 1
    parent, prefix = expanded[:cut], expanded[cut:]
    head = raw[: len(raw) - len(prefix)]
    found: list[str] = []
    try:
        with os.scandir(parent) as it:
            for n, entry in enumerate(it):
                if n >= _MAX_SCANNED:
                    break
                name = entry.name
                if name.startswith(".") and not prefix.startswith("."):
                    continue
                if not name.lower().startswith(prefix.lower()):
                    continue
                if not entry.is_dir():
                    continue
                if roots is not None:
                    path = Path(entry.path).resolve()
                    if not any(
                        path.is_relative_to(r) or r.is_relative_to(path) for r in roots
                    ):
                        continue
                found.append(name)
    except OSError:
        return []
    found.sort(key=str.lower)
    return [head + name for name in found[:_MAX_SUGGESTIONS]]


def build_workspaces_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()
    store = deps.store

    def require_switch() -> None:
        if deps.workspace_switch is None:
            raise WebError(
                404,
                "feature_unavailable",
                "this server doesn't let chats pick a directory",
            )

    def agent_of() -> Agent[Any]:
        """The agent the list is for: the default one, else the first with a
        local workspace (a new chat's directory defaults to its own)."""
        default = deps.default_agent
        candidates = [deps.agents[default]] if default else []
        candidates += list(deps.agents.values())
        for agent in candidates:
            if own_root(agent) is not None:
                return agent
        raise WebError(
            404,
            "feature_unavailable",
            "no agent with a local workspace is served",
            hint="configure a model first (Settings → Models)",
        )

    def fixed(agent: Agent[Any]) -> list[WorkspaceItem]:
        switch = deps.workspace_switch
        assert switch is not None
        items: list[WorkspaceItem] = []
        own = own_root(agent)
        if own is not None:
            items.append(
                WorkspaceItem(
                    root=own.as_posix(), name=own.name or str(own), kind="default"
                )
            )
        general = switch.general
        if isinstance(general, Path) and general != own:
            items.append(
                WorkspaceItem(
                    root=general.as_posix(), name=general.name, kind="general"
                )
            )
        return items

    def item_of(agent: Agent[Any], root: str, *, row: Any = None) -> WorkspaceItem:
        for item in fixed(agent):
            if item.root == root:
                return item
        return WorkspaceItem(
            id=row.id if row else None,
            root=root,
            name=row.name if row else Path(root).name,
            last_used_at=row.last_used_at if row else None,
        )

    @router.get("/api/workspaces", response_model=WorkspaceList)
    async def list_workspaces() -> WorkspaceList:
        require_switch()
        switch = deps.workspace_switch
        assert switch is not None
        agent = agent_of()
        own = own_root(agent)
        items = fixed(agent)
        shown = {i.root for i in items}
        for row in await store.list_workspaces():
            # A directory a narrower deployment no longer allows isn't offered.
            if row.root in shown or switch.refusal(Path(row.root), own) is not None:
                continue
            items.append(item_of(agent, row.root, row=row))
        roots = switch.roots
        return WorkspaceList(
            workspaces=items,
            default=own.as_posix() if own else None,
            can_add=roots is not None,
            roots=(
                None
                if roots is None or roots == "any"
                else [Path(r).as_posix() for r in roots]
            ),
        )

    @router.post("/api/workspaces", response_model=WorkspaceItem)
    async def add_workspace(req: WorkspaceAdd) -> WorkspaceItem:
        """Add a directory to the list (symlinks resolved; it must exist and
        be allowed here)."""
        require_switch()
        agent = agent_of()
        try:
            path = deps.admit_root(agent, req.path.strip())
        except WebError as exc:
            if exc.code == "workspace_missing":
                raise WebError(
                    404,
                    "file_not_found",
                    f"no such directory: {req.path.strip()}",
                    hint="check the path; it must be an existing directory",
                ) from None
            raise
        row = await store.touch_workspace(path.as_posix())
        return item_of(agent, row.root, row=row)

    @router.delete("/api/workspaces/{workspace_id}")
    async def remove_workspace(workspace_id: str) -> dict[str, bool]:
        """Drop a directory from the list. Its files and chats stay; using
        it again (a new chat there) lists it again."""
        require_switch()
        if not await store.remove_workspace(workspace_id):
            raise WebError(
                404,
                "workspace_not_found",
                "no such entry in the workspace list",
                hint="the directories always listed can't be removed",
            )
        return {"ok": True}

    @router.get("/api/workspaces/dirs", response_model=list[str])
    async def complete_dir(path: str = Query("", max_length=4096)) -> list[str]:
        """Completions for a directory path being typed: subdirectory paths
        as typed (``~`` kept), within what this server allows to be added."""
        require_switch()
        switch = deps.workspace_switch
        assert switch is not None
        if switch.roots is None:
            return []
        roots = None if switch.roots == "any" else tuple(Path(r) for r in switch.roots)
        return await asyncio.to_thread(_suggest, path or "~/", roots)

    @router.post("/api/sessions/associate", response_model=AssociateResponse)
    async def associate_sessions(req: AssociateRequest) -> AssociateResponse:
        """Bind chats that have no directory yet (from before chats had one)
        to ``root``, for good. A chat already bound is left as it is."""
        require_switch()
        root, bound = await deps.associate(req.session_ids, req.root)
        return AssociateResponse(associated=list(bound), root=root)

    return router
