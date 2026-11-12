"""Workspace routes: the directories a chat may work in.

``GET /api/workspaces`` is the page's list — the agent's own directory and
the switch's general one first (always there), then every directory added or
used, most recent first. The list is interface state: what a chat may open
is decided by :meth:`RouterDeps.admit_root` on every run, never by it.
Chats from before chats had a directory are bound to one here too.
"""

from __future__ import annotations

import asyncio
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
    WorkspaceRename,
)
from ...exceptions import UserError
from ...workspace import WorkspaceError
from ...workspace.paths import complete_dirs
from ..workspaces import WorkspaceRef, own_root
from .deps import RouterDeps


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
            host=WorkspaceRef.parse(root).host_name,
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
        shown = {i.root: i for i in items}
        for row in await store.list_workspaces():
            if row.root in shown:
                # Always listed; its row holds the name it was given, if any
                # ("General" stays the page's word).
                if shown[row.root].kind == "default":
                    shown[row.root].name = row.name
                continue
            # A place a narrower deployment no longer allows isn't offered.
            ref = WorkspaceRef.parse(row.root)
            refused = (
                switch.remote_refusal(ref)
                if ref.remote
                else switch.refusal(Path(row.root), own)
            )
            if refused is not None:
                continue
            items.append(item_of(agent, row.root, row=row))
        roots = switch.roots
        return WorkspaceList(
            workspaces=items,
            default=own.as_posix() if own else None,
            can_add=roots is not None,
            remotes=(
                None
                if switch.remotes is None
                else "any"
                if switch.remotes == "any"
                else list(switch.remotes)
            ),
            roots=(
                None
                if roots is None or roots == "any"
                else [Path(r).as_posix() for r in roots]
            ),
        )

    @router.post("/api/workspaces", response_model=WorkspaceItem)
    async def add_workspace(req: WorkspaceAdd) -> WorkspaceItem:
        """Add a directory to the list (symlinks resolved; it must exist and
        be allowed here). ``path`` may name a remote location —
        ``ssh://box/srv/project``, ``docker://api/app`` — which is looked up
        on its host."""
        require_switch()
        agent = agent_of()
        try:
            key = await deps.admit_new(agent, req.path.strip())
        except WebError as exc:
            if exc.code == "workspace_missing":
                raise WebError(
                    404,
                    "file_not_found",
                    f"no such directory: {req.path.strip()}",
                    hint="check the path; it must be an existing directory",
                ) from None
            raise
        row = await store.touch_workspace(key)
        return item_of(agent, row.root, row=row)

    @router.patch("/api/workspaces", response_model=WorkspaceItem)
    async def rename_workspace(req: WorkspaceRename) -> WorkspaceItem:
        """Give a listed directory a display name (two folders may share
        one); empty restores the folder's own. Its path is not touched."""
        listed = {i.root: i for i in (await list_workspaces()).workspaces}
        item = listed.get(req.root)
        if item is None or item.kind == "general":
            raise WebError(
                404,
                "workspace_not_found",
                "no such entry in the workspace list",
                hint="only a listed directory can be renamed (not the general one)",
            )
        row = await store.rename_workspace(req.root, req.name)
        return item.model_copy(update={"name": row.name})

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
    async def complete_dir(
        path: str = Query("", max_length=4096),
        host: str | None = Query(None, max_length=512),
    ) -> list[str]:
        """Completions for a directory path being typed: subdirectory paths
        as typed (``~`` kept), within what this server allows to be added.

        With ``host`` — the front of a remote location's key, ``ssh://box`` —
        the folders are that host's. Nothing to suggest (a host that can't
        be reached included) is an empty list: this only feeds a picker, and
        adding the location is what reports why.
        """
        require_switch()
        switch = deps.workspace_switch
        assert switch is not None
        if host is not None:
            ref = WorkspaceRef.parse(f"{host.rstrip('/')}/x")
            if not ref.remote or switch.remote_refusal(ref) is not None:
                return []
            try:
                return await switch.remote_host(ref).complete_dirs(path or "~/")
            except (UserError, WorkspaceError, OSError):
                return []
        if switch.roots is None:
            return []
        roots = None if switch.roots == "any" else tuple(Path(r) for r in switch.roots)
        return await asyncio.to_thread(complete_dirs, path or "~/", roots)

    @router.post("/api/sessions/associate", response_model=AssociateResponse)
    async def associate_sessions(req: AssociateRequest) -> AssociateResponse:
        """Bind chats that have no directory yet (from before chats had one)
        to ``root``, for good. A chat already bound is left as it is."""
        require_switch()
        root, bound = await deps.associate(req.session_ids, req.root)
        return AssociateResponse(associated=list(bound), root=root)

    return router
