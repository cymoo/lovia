"""Workspace routes: the Files panel's read-only window into an agent's files.

lovia web is a personal assistant, so the panel's first job is "where is the
thing the assistant just made" — a recency-sorted flat list — with breadcrumb
browsing, text content, and raw bytes (image preview / download) behind it.

Every read runs through a session forced to a **readonly** policy built from
the agent's workspace config. The agent's own mode may answer ``ask`` for
approval-gated operations, but a web GET has no approval flow — under the
readonly preset everything outside the root (and everything the agent's
``denied_paths`` hide, e.g. ``.env*``) is a plain ``deny``, which the session
raises as :class:`PermissionDeniedError`. The panel can never see more than
the agent itself could read without asking.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import FileResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ...exceptions import UserError
from ...workspace import (
    LocalWorkspace,
    LocalWorkspaceSession,
    PermissionDeniedError,
    WorkspaceError,
    WorkspacePolicy,
)
from ...workspace.paths import resolve_path
from ..schemas import WorkspaceEntry, WorkspaceFile, WorkspaceInfo
from .deps import RouterDeps


def workspace_cfg(agent: Agent[Any]) -> LocalWorkspace | None:
    """The agent's local-workspace config, or None when the panel can't serve.

    ``Agent.workspace`` may be a ``LocalWorkspace`` or a session binding whose
    ``.workspace`` points back at one; any other ``WorkspaceLike`` has no
    root/policy semantics we can browse.
    """
    ws = agent.workspace
    cfg = getattr(ws, "workspace", ws)
    return cfg if isinstance(cfg, LocalWorkspace) else None


def _view_session(cfg: LocalWorkspace) -> LocalWorkspaceSession:
    """A per-request session locked to readonly.

    Carries over only ``denied_paths`` from the agent's policy — never its
    ``path_rules``, which could *widen* access (e.g. allow reads outside the
    root that plain readonly denies).
    """
    return LocalWorkspaceSession(
        root=cfg.root,
        policy=WorkspacePolicy.readonly(denied_paths=cfg.policy.denied_paths),
        limits=cfg.limits,
    )


def _root_of(cfg: LocalWorkspace) -> Path:
    # Same normalization LocalWorkspaceSession applies to its root.
    return Path(cfg.root).expanduser().resolve()


# Lines per /api/workspace/file page — small enough that the session's char
# cap (50k) rarely clips within one page, so `truncated` ≈ "more lines exist".
_PAGE_LINES = 500


async def _sniff_binary(abs_path: Path) -> bool:
    """True when the first KB smells binary (same heuristic grep uses)."""

    def _impl() -> bool:
        try:
            with abs_path.open("rb") as fh:
                return b"\0" in fh.read(1024)
        except OSError:
            return False

    return await asyncio.to_thread(_impl)


def build_workspace_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    def require_cfg(agent_name: str | None) -> LocalWorkspace:
        agent = deps.pick(agent_name)
        cfg = workspace_cfg(agent)
        if cfg is None:
            raise HTTPException(status_code=404, detail="agent has no workspace")
        return cfg

    def entry_out(e: Any) -> WorkspaceEntry:
        return WorkspaceEntry(
            path=e.path,
            is_dir=e.is_dir,
            size=e.size,
            mtime=e.mtime,
            symlink_target=e.symlink_target,
        )

    @router.get("/api/workspace", response_model=WorkspaceInfo)
    async def workspace_info(agent: str | None = Query(None)) -> WorkspaceInfo:
        cfg = require_cfg(agent)
        return WorkspaceInfo(name=_root_of(cfg).name or "workspace")

    @router.get("/api/workspace/files", response_model=list[WorkspaceEntry])
    async def list_dir(
        agent: str | None = Query(None),
        path: str = Query(".", max_length=4096),
    ) -> list[WorkspaceEntry]:
        """One directory level (breadcrumb browsing), dirs first."""
        cfg = require_cfg(agent)
        try:
            async with _view_session(cfg) as session:
                entries = await session.list_files(path)
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # "Not a directory: …" / a vanished workspace root both land here.
        except (WorkspaceError, UserError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [entry_out(e) for e in entries]

    @router.get("/api/workspace/recent", response_model=list[WorkspaceEntry])
    async def recent_files(
        agent: str | None = Query(None),
        limit: int = Query(100, ge=1, le=500),
    ) -> list[WorkspaceEntry]:
        """Files across the whole workspace, newest first.

        Approximate on huge workspaces: the underlying walk caps at the
        workspace's ``max_list_results`` (500 by default) before sorting —
        fine for the personal-assistant scale this UI targets.
        """
        cfg = require_cfg(agent)
        try:
            async with _view_session(cfg) as session:
                entries = await session.list_files(".", pattern="**/*")
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (WorkspaceError, UserError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        files = [e for e in entries if not e.is_dir]
        files.sort(key=lambda e: e.mtime or 0.0, reverse=True)
        return [entry_out(e) for e in files[:limit]]

    @router.get("/api/workspace/file", response_model=WorkspaceFile)
    async def read_file(
        agent: str | None = Query(None),
        path: str = Query(..., max_length=4096),
        start: int = Query(1, ge=1),
    ) -> WorkspaceFile:
        """Text content in fixed line pages; flags binaries instead of decoding.

        The endpoint owns the page size: ``read_text`` treats ``end`` as the
        *requested* range (its char clipping doesn't move ``end``), so an
        explicit window keeps ``end``/``truncated`` an honest has-more signal
        for the viewer's Load-more.
        """
        cfg = require_cfg(agent)
        try:
            async with _view_session(cfg) as session:
                if session.decide_path(path) != "allow":
                    raise HTTPException(status_code=403, detail="path not readable")
                resolved = resolve_path(_root_of(cfg), path)
                if not resolved.abs.is_file():
                    raise HTTPException(status_code=404, detail="no such file")
                if await _sniff_binary(resolved.abs):
                    return WorkspaceFile(
                        path=resolved.display(), content="", binary=True
                    )
                content = await session.read_text(
                    path, start=start, end=start + _PAGE_LINES - 1
                )
        except PermissionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (IsADirectoryError, WorkspaceError, UserError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return WorkspaceFile(
            path=content.path,
            content=content.content,
            start=content.start,
            end=content.end,
            total_lines=content.total_lines,
            truncated=content.truncated,
        )

    @router.get("/api/workspace/raw")
    async def raw_file(
        agent: str | None = Query(None),
        path: str = Query(..., max_length=4096),
        download: bool = Query(False),
    ) -> FileResponse:
        """Raw bytes: inline for images (viewer preview), attachment for any
        file when ``download=1`` — "take the file the assistant made" is a
        first-class action for an assistant UI."""
        cfg = require_cfg(agent)
        try:
            async with _view_session(cfg) as session:
                if session.decide_path(path) != "allow":
                    raise HTTPException(status_code=403, detail="path not readable")
        except UserError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        resolved = resolve_path(_root_of(cfg), path)
        if not resolved.abs.is_file():
            raise HTTPException(status_code=404, detail="no such file")
        try:
            size = resolved.abs.stat().st_size
        except OSError as exc:  # vanished between the is_file check and here
            raise HTTPException(status_code=404, detail="no such file") from exc
        if size > cfg.limits.max_file_read_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        media_type = mimetypes.guess_type(resolved.abs.name)[0]
        if not download and not (media_type or "").startswith("image/"):
            raise HTTPException(status_code=415, detail="inline preview is images-only")
        return FileResponse(
            resolved.abs,
            media_type=media_type or "application/octet-stream",
            filename=resolved.abs.name if download else None,
            content_disposition_type="attachment" if download else "inline",
        )

    return router
