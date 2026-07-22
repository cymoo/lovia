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
the agent itself could read without asking — and strictly less: regenerable
environment junk (:data:`_PANEL_IGNORES`) is hidden panel-wide so recency
stays about the user's actual files.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

try:
    from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
    from fastapi.responses import FileResponse, Response
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
from ..attachments import INLINE_IMAGE_MIME
from ..schemas import UploadedFile, WorkspaceEntry, WorkspaceFile, WorkspaceInfo
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


# Junk the panel hides everywhere (Recent, browsing, preview): regenerable
# environment/cache noise whose ever-fresh mtimes would otherwise dominate
# the recency sort — and eat the walk's result cap before real files do.
# Dot-prefixed junk (.git, .venv) is already hidden by the dotfile rule, and
# build *outputs* (dist/ etc.) stay visible: "take the file the assistant
# made" is the panel's job. Same pattern language as ``denied_paths``: a bare
# name matches the file or directory (and everything beneath it) at any depth.
_PANEL_IGNORES: tuple[str, ...] = ("__pycache__", "*.pyc", "venv", "node_modules")


def _view_session(cfg: LocalWorkspace) -> LocalWorkspaceSession:
    """A per-request session locked to readonly.

    Carries over only ``denied_paths`` from the agent's policy — never its
    ``path_rules``, which could *widen* access (e.g. allow reads outside the
    root that plain readonly denies) — and adds the panel's own junk filter
    (``_PANEL_IGNORES``). Denied entries are skipped before the walk's result
    cap, so junk cannot crowd real files out of the Recent list either.
    """
    return LocalWorkspaceSession(
        root=cfg.root,
        policy=WorkspacePolicy.readonly(
            denied_paths=cfg.policy.denied_paths + _PANEL_IGNORES
        ),
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


# Composer/Files-panel uploads land here, under the workspace root, so they
# are immediately servable via /api/workspace/raw and visible in the panel.
_UPLOADS_SUBDIR = "uploads"
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MiB


def _safe_upload_name(raw: str) -> str:
    """A filesystem-safe basename: strip any directory, keep readable chars.

    ``\\w`` is Unicode-aware, so CJK filenames survive; separators and control
    characters collapse to ``_``. The result is always a plain basename, which
    (together with writing only under ``uploads/``) prevents path traversal.
    """
    base = Path(raw).name.strip()
    base = re.sub(r"[^\w.\- ]+", "_", base).strip(". ")
    return (base or "upload")[:120]


def _write_upload(uploads: Path, name: str, data: bytes) -> Path:
    """Create ``uploads/`` and write ``data`` to a collision-free target.

    Uses exclusive creation (``O_EXCL``) so two concurrent uploads that pick the
    same name can't clobber each other — the loser just advances to the next
    suffix instead of overwriting (no check-then-write TOCTOU window).
    """
    uploads.mkdir(parents=True, exist_ok=True)
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 0
    while True:
        target = uploads / (name if i == 0 else f"{stem}-{i}{suffix}")
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            i += 1
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return target


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
        request: Request,
        agent: str | None = Query(None),
        path: str = Query(..., max_length=4096),
        download: bool = Query(False),
    ) -> Response:
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
            stat_result = resolved.abs.stat()
        except OSError as exc:  # vanished between the is_file check and here
            raise HTTPException(status_code=404, detail="no such file") from exc
        if stat_result.st_size > cfg.limits.max_file_read_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        media_type = mimetypes.guess_type(resolved.abs.name)[0]
        # Inline only raster images. SVG is an image type but can carry scripts,
        # so it is never served inline (a crafted upload could otherwise be a
        # stored-XSS vector); it stays reachable via download=1.
        inline_ok = (media_type or "").startswith("image/") and (
            media_type != "image/svg+xml"
        )
        if not download and not inline_ok:
            raise HTTPException(status_code=415, detail="inline preview is images-only")
        # `no-cache` = cache but revalidate: the viewer re-opens the same URL
        # constantly (and re-renders after every agent edit), so unchanged
        # files answer 304 from the ETag below instead of re-sending bytes —
        # and changed files show fresh without any query-string busting.
        response = FileResponse(
            resolved.abs,
            media_type=media_type or "application/octet-stream",
            stat_result=stat_result,
            filename=resolved.abs.name if download else None,
            content_disposition_type="attachment" if download else "inline",
            headers={"cache-control": "no-cache"},
        )
        etag = response.headers.get("etag")
        if_none_match = request.headers.get("if-none-match")
        if etag and if_none_match:
            candidates = {
                stripped.removeprefix("W/")
                for t in if_none_match.split(",")
                if (stripped := t.strip())
            }
            # "*" matches any current representation (RFC 9110 §13.1.2).
            if "*" in candidates or etag.removeprefix("W/") in candidates:
                return Response(
                    status_code=304,
                    headers={"etag": etag, "cache-control": "no-cache"},
                )
        return response

    @router.post("/api/workspace/upload", response_model=UploadedFile)
    async def upload_file(
        file: UploadFile = File(...),
        agent: str | None = Query(None),
    ) -> UploadedFile:
        """Save a composer/Files-panel upload under the workspace ``uploads/``.

        Requires a local workspace (same 404 gate as the panel). Bytes are read
        with a hard cap so a huge upload can't exhaust memory, then written to a
        collision-free name; the saved file is immediately servable via
        ``/api/workspace/raw`` and referenced from chat by its workspace path.
        """
        cfg = require_cfg(agent)
        uploads = _root_of(cfg) / _UPLOADS_SUBDIR
        try:
            data = bytearray()
            while chunk := await file.read(1 << 20):
                data.extend(chunk)
                if len(data) > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="file too large")
            if not data:
                raise HTTPException(status_code=422, detail="empty file")
            name = _safe_upload_name(file.filename or "upload")
            target = await asyncio.to_thread(_write_upload, uploads, name, bytes(data))
        finally:
            await file.close()
        # Type comes from the SAVED extension, never the client's Content-Type;
        # kind="image" is limited to raster formats the raw endpoint will inline
        # (SVG is excluded — it can carry scripts), so the composer previews only
        # what is safe to render.
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return UploadedFile(
            path=f"{_UPLOADS_SUBDIR}/{target.name}",
            name=target.name,
            mime=mime,
            kind="image" if mime in INLINE_IMAGE_MIME else "file",
            size=len(data),
        )

    return router
