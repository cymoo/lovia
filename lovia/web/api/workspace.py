"""Workspace routes: the Files panel's read-only window into an agent's files.

lovia web is a personal assistant, so the panel's first job is "what did the
assistant change" — the chat's own files, plus what else git sees
uncommitted (:mod:`lovia.web.changes`), each with its diff — with breadcrumb
browsing, text content, and raw bytes (image preview / download) behind it.

Every read runs through a session forced to a **readonly** policy built from
the agent's workspace config (:func:`~lovia.web.workspaces.open_view`). The
agent's own mode may answer ``ask`` for approval-gated operations, but a web
GET has no approval flow — under the readonly preset everything outside the
root (and everything the agent's ``denied_paths`` hide, e.g. ``.env*``) is a
plain ``deny``, which the session raises as :class:`PermissionDeniedError`.
The panel can never see more than the agent itself could read without
asking. That is the only access rule: what the change list leaves out (the
scratch dir) is presentation, so a path the panel doesn't surface — an
inline image the assistant wrote under ``tmp/``, a tool card's "open in
Files" — still opens when asked for by name.

The session is opened where the files are, so a chat working on a remote
host is served by the same routes: nothing here touches this machine's disk
except ``raw`` for a local file, which keeps ``FileResponse`` (ranges,
``sendfile``) instead of carrying bytes through the session.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import mimetypes
import os
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

try:
    from fastapi import APIRouter, File, Query, Request, UploadFile
    from fastapi.responses import FileResponse, Response, StreamingResponse
    from starlette.background import BackgroundTask
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...exceptions import UserError
from ...workspace import (
    SCRATCH_DIR,
    FileTooLargeError,
    LocalWorkspace,
    PermissionDeniedError,
    RemoteWorkspace,
    WorkspaceDisconnectedError,
    WorkspaceError,
)
from ...workspace.paths import resolve_path
from ...workspace.protocol import WorkspaceSession
from .. import changes
from ..errors import WebError
from ..media import is_preview_image, preview_image_mime
from ..schemas import (
    ChangesRequest,
    ProcessInfo,
    UploadedFile,
    WorkspaceChanges,
    WorkspaceDiff,
    WorkspaceEntry,
    WorkspaceFile,
    WorkspaceInfo,
)
from ..workspaces import ChatBinding, WorkspaceRef, files_config, open_view
from .deps import RouterDeps

FilesConfig = LocalWorkspace | RemoteWorkspace


def _io_error(exc: OSError) -> WebError:
    """What the system itself refused or could not do (no permission, a full
    disk) — raised here for a local workspace, sent back by the host for a
    remote one."""
    if isinstance(exc, PermissionError):
        return WebError.from_exc(403, "path_denied", exc)
    return WebError.from_exc(500, "workspace_io_error", exc)


@asynccontextmanager
async def _view(
    cfg: FilesConfig, *, write: bool = False, git: bool = False
) -> AsyncIterator[WorkspaceSession]:
    """A per-request :func:`~lovia.web.workspaces.open_view` session, with
    what goes wrong in it said as an HTTP error.

    Only an upload passes ``write``; only the change list and diff ``git``.
    """
    try:
        session = await open_view(cfg, write=write, git=git)
    except UserError as exc:  # the root is gone (moved, deleted)
        raise WebError.from_exc(404, "file_not_found", exc) from exc
    except WorkspaceError as exc:  # no way through to the host
        raise WebError.from_exc(502, "workspace_unreachable", exc) from exc
    except OSError as exc:
        raise _io_error(exc) from exc
    try:
        yield session
    except PermissionDeniedError as exc:
        raise WebError.from_exc(403, "path_denied", exc) from exc
    except FileTooLargeError as exc:
        raise WebError.from_exc(413, "file_too_large", exc) from exc
    except WorkspaceDisconnectedError as exc:
        raise WebError.from_exc(502, "workspace_unreachable", exc) from exc
    except asyncio.TimeoutError as exc:
        raise WebError(
            502, "workspace_unreachable", "the workspace's host stopped answering"
        ) from exc
    # "Not a file: …", "Not a directory: …", a path that isn't there.
    except (
        FileNotFoundError,
        NotADirectoryError,
        IsADirectoryError,
        WorkspaceError,
        UserError,
    ) as exc:
        raise WebError.from_exc(404, "file_not_found", exc) from exc
    except OSError as exc:
        raise _io_error(exc) from exc
    finally:
        await session.close()


async def _must_read(session: WorkspaceSession, path: str) -> None:
    """403 unless ``path`` reads without asking — stricter than the session
    itself, which lets an ``ask`` through for the tool layer to settle."""
    verdict = session.decide_path(path)
    if inspect.isawaitable(verdict):  # a remote session asks its host
        verdict = await verdict
    if verdict.action != "allow":
        raise WebError(403, "path_denied", "path not readable")


def _root_name(cfg: FilesConfig) -> str:
    if isinstance(cfg, RemoteWorkspace):
        return PurePosixPath(cfg.root).name or cfg.host.name
    # Same normalization LocalWorkspaceSession applies to its root.
    return Path(cfg.root).expanduser().resolve().name or "workspace"


# Lines per /api/workspace/file page — small enough that the session's char
# cap (50k) rarely clips within one page, so `truncated` ≈ "more lines exist".
_PAGE_LINES = 500

# How much of a file tells text from binary (the heuristic grep uses).
_SNIFF_BYTES = 1024

# One piece of a file read from a session for ``raw``. Each is a message of
# its own over a remote connection: small enough that a slow link shows
# progress (and keeps answering other requests in between), large enough that
# the round trip per piece doesn't matter.
_RAW_CHUNK = 256 * 1024


def _etag(size: int, mtime: float | None) -> str:
    """A validator for a file known only by its size and mtime — what
    ``FileResponse`` derives its own from."""
    return f'"{hashlib.md5(f"{mtime}-{size}".encode(), usedforsecurity=False).hexdigest()}"'


def _not_modified(request: Request, etag: str | None) -> bool:
    if_none_match = request.headers.get("if-none-match")
    if not etag or not if_none_match:
        return False
    candidates = {
        stripped.removeprefix("W/")
        for t in if_none_match.split(",")
        if (stripped := t.strip())
    }
    # "*" matches any current representation (RFC 9110 §13.1.2).
    return "*" in candidates or etag.removeprefix("W/") in candidates


def _attachment(name: str) -> str:
    """A ``Content-Disposition`` that downloads as ``name`` (RFC 6266)."""
    quoted = quote(name)
    if quoted == name:
        return f'attachment; filename="{name}"'
    return f"attachment; filename*=utf-8''{quoted}"


# Composer/Files-panel uploads land here, under the workspace root, so they
# are immediately servable via /api/workspace/raw and visible in the panel.
_UPLOADS_SUBDIR = "uploads"
_DEFAULT_MAX_UPLOAD_MB = 25
# Extensions accepted by default — images, documents, data, and text/code a
# personal assistant routinely handles. Override with LOVIA_UPLOAD_ALLOWED_EXT
# (comma/space separated, no dots); "*" accepts any type. Files with no
# extension (READMEs, Makefiles, ...) are always allowed.
_DEFAULT_ALLOWED_UPLOAD_EXT = frozenset(
    "jpg jpeg png gif webp avif bmp ico svg heic heif tiff "
    "pdf txt md markdown rst rtf csv tsv json jsonl ndjson yaml yml xml "
    "toml ini cfg conf log "
    "doc docx xls xlsx ppt pptx odt ods odp "
    "py pyi js mjs cjs ts tsx jsx html htm css scss sass "
    "sh bash zsh fish sql c h cpp hpp cc cxx java kt go rs rb php swift "
    "lua r m scala clj ex exs vue svelte "
    "zip tar gz tgz bz2 xz 7z parquet ipynb".split()
)


def _max_upload_bytes() -> int:
    """Upload size cap in bytes (env ``LOVIA_MAX_UPLOAD_MB``, default 25)."""
    raw = os.environ.get("LOVIA_MAX_UPLOAD_MB")
    mb = _DEFAULT_MAX_UPLOAD_MB
    if raw:
        try:
            mb = max(1, int(raw))
        except ValueError:
            pass
    return mb * 1024 * 1024


def _allowed_upload_ext() -> frozenset[str] | None:
    """Allowed upload extensions (env ``LOVIA_UPLOAD_ALLOWED_EXT``); ``None``
    means "any" (the var set to ``*`` or empty)."""
    raw = os.environ.get("LOVIA_UPLOAD_ALLOWED_EXT")
    if raw is None:
        return _DEFAULT_ALLOWED_UPLOAD_EXT
    raw = raw.strip()
    if not raw or raw == "*":
        return None
    return frozenset(e.lower().lstrip(".") for e in re.split(r"[,\s]+", raw) if e)


def _safe_upload_name(raw: str) -> str:
    """A filesystem-safe basename: strip any directory, keep readable chars.

    ``\\w`` is Unicode-aware, so CJK filenames survive; separators and control
    characters collapse to ``_``. The result is always a plain basename, which
    (together with writing only under ``uploads/``) prevents path traversal.
    """
    base = Path(raw).name.strip()
    base = re.sub(r"[^\w.\- ]+", "_", base).strip(". ")
    return (base or "upload")[:120]


async def _write_upload(
    session: WorkspaceSession, name: str, data: bytes
) -> tuple[str, str]:
    """Write ``data`` under ``uploads/`` at a collision-free name; returns
    the path written and its basename.

    A name is probed before it is written to: over a remote connection each
    write attempt carries the whole file. The write itself still creates
    exclusively, so two concurrent uploads that settle on one name can't
    clobber each other — the loser moves on to the next suffix.
    """
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 0
    while True:
        candidate = name if i == 0 else f"{stem}-{i}{suffix}"
        rel = f"{_UPLOADS_SUBDIR}/{candidate}"
        i += 1
        try:
            await session.stat(rel)
        except FileNotFoundError:
            written = await session.write_bytes(rel, data, create_only=True)
            if written.ok:
                return rel, candidate


def build_workspace_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    async def require_cfg(
        agent_name: str | None, session: str | None = None, root: str | None = None
    ) -> FilesConfig:
        """The workspace config to serve: the chat's directory with
        ``session``, ``root`` (a draft's, checked like a new chat's) with
        that, else the agent's own."""
        if session is not None:
            meta = await deps.store.get(session)
            if meta is None:
                raise WebError(404, "session_not_found", "session not found")
            agent = deps.pick(deps.agent_of(meta))
            root = meta.workspace_root
        else:
            agent = deps.pick(agent_name)
        cfg = files_config(agent)
        if cfg is None:
            raise WebError(404, "feature_unavailable", "agent has no workspace")
        # A chat only has a place of its own under an agent whose workspace
        # is local (see RouterDeps.chat_binding).
        if root is None or not isinstance(cfg, LocalWorkspace):
            return cfg
        if not WorkspaceRef.parse(root).remote:
            return replace(cfg, root=deps.admit_root(agent, root).as_posix())
        # Checked as a run there is, then built as one's workspace is: the
        # host its chats are connected to, the agent's limits and denials.
        there = ChatBinding(deps.admit(agent, root), None)
        remote = files_config(deps.at_root(agent, there))
        assert remote is not None
        return remote

    def entry_out(e: Any) -> WorkspaceEntry:
        return WorkspaceEntry(
            path=e.path,
            is_dir=e.is_dir,
            size=e.size,
            mtime=e.mtime,
            symlink_target=e.symlink_target,
        )

    @router.get("/api/workspace", response_model=WorkspaceInfo)
    async def workspace_info(
        agent: str | None = Query(None),
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
    ) -> WorkspaceInfo:
        cfg = await require_cfg(agent, chat, root)
        return WorkspaceInfo(name=_root_name(cfg), scratch_dir=SCRATCH_DIR)

    @router.get("/api/workspace/files", response_model=list[WorkspaceEntry])
    async def list_dir(
        agent: str | None = Query(None),
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
        path: str = Query(".", max_length=4096),
    ) -> list[WorkspaceEntry]:
        """One directory level (breadcrumb browsing), dirs first."""
        cfg = await require_cfg(agent, chat, root)
        async with _view(cfg) as session:
            entries = await session.list_files(path)
        return [entry_out(e) for e in entries]

    @router.post("/api/workspace/changes", response_model=WorkspaceChanges)
    async def list_changes(
        body: ChangesRequest,
        agent: str | None = Query(None),
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
    ) -> WorkspaceChanges:
        """The chat's own files (``paths``, as its tool calls spelled them),
        then the work tree's other uncommitted changes when the root is in
        git. Costs one ``stat`` per path and at most three git commands —
        never a walk of the tree."""
        cfg = await require_cfg(agent, chat, root)
        async with _view(cfg, git=True) as session:
            return await changes.list_changes(session, body.paths)

    @router.get("/api/workspace/diff", response_model=WorkspaceDiff)
    async def file_diff(
        agent: str | None = Query(None),
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
        path: str = Query(..., min_length=1, max_length=4096),
        old: str | None = Query(None, max_length=4096),
    ) -> WorkspaceDiff:
        """One file against ``HEAD``, unified, cut to the workspace's shell
        output limit. ``old`` names a rename's former path."""
        cfg = await require_cfg(agent, chat, root)
        async with _view(cfg, git=True) as session:
            await _must_read(session, path)
            if old:
                await _must_read(session, old)
            diff = await changes.file_diff(
                session,
                path,
                old=old,
                limit=cfg.limits.max_shell_output_chars,
                # A local workspace's commands run in this machine's shell.
                windows=isinstance(cfg, LocalWorkspace) and os.name == "nt",
            )
        if diff is None:
            raise WebError(404, "feature_unavailable", "no git history to diff against")
        return diff

    @router.get("/api/workspace/file", response_model=WorkspaceFile)
    async def read_file(
        agent: str | None = Query(None),
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
        path: str = Query(..., max_length=4096),
        start: int = Query(1, ge=1),
    ) -> WorkspaceFile:
        """Text content in fixed line pages; flags binaries instead of decoding.

        The endpoint owns the page size: ``read_text`` treats ``end`` as the
        *requested* range (its char clipping doesn't move ``end``), so an
        explicit window keeps ``end``/``truncated`` an honest has-more signal
        for the viewer's Load-more.
        """
        cfg = await require_cfg(agent, chat, root)
        async with _view(cfg) as session:
            await _must_read(session, path)
            head = await session.read_bytes(path, length=_SNIFF_BYTES)
            if b"\0" in head.data:
                return WorkspaceFile(path=head.path, content="", binary=True)
            content = await session.read_text(
                path, start=start, end=start + _PAGE_LINES - 1
            )
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
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
        path: str = Query(..., max_length=4096),
        download: bool = Query(False),
    ) -> Response:
        """Raw bytes: inline for images and PDFs (viewer preview), attachment
        for any file when ``download=1`` — "take the file the assistant made"
        is a first-class action for an assistant UI."""
        cfg = await require_cfg(agent, chat, root)
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(_view(cfg))
            await _must_read(session, path)
            if isinstance(cfg, LocalWorkspace):
                return _raw_local(cfg, path, request, download)
            entry = await session.stat(path)
            if entry.is_dir or entry.size is None:
                raise WebError(404, "file_not_found", "no such file")
            name, size = PurePosixPath(entry.path).name, entry.size
            media_type, headers = _raw_head(cfg, name, size, download)
            headers["etag"] = _etag(size, entry.mtime)
            if _not_modified(request, headers["etag"]):
                return Response(status_code=304, headers=headers)
            headers["content-length"] = str(size)
            if download:
                headers["content-disposition"] = _attachment(name)
            # The session outlives this function: the body is read through it
            # a piece at a time, as the client takes it.
            held = stack.pop_all()

        async def pieces() -> AsyncIterator[bytes]:
            async with held:
                sent = 0
                while sent < size:
                    piece = await session.read_bytes(
                        path, offset=sent, length=min(_RAW_CHUNK, size - sent)
                    )
                    if not piece.data:
                        return  # it shrank under us: the client sees it cut short
                    sent += len(piece.data)
                    yield piece.data

        return StreamingResponse(
            pieces(),
            media_type=media_type,
            headers=headers,
            # For a body that is never started (the client already gone).
            background=BackgroundTask(held.aclose),
        )

    def _raw_head(
        cfg: FilesConfig, name: str, size: int, download: bool
    ) -> tuple[str, dict[str, str]]:
        """The media type and headers ``raw`` answers with for a file called
        ``name`` — once it is one ``raw`` serves at all."""
        if size > cfg.limits.max_file_read_bytes:
            raise WebError(413, "file_too_large", "file too large")
        # Prefer the explicit preview map so a nosniff'd inline image carries a
        # correct, OS-stable Content-Type; fall back to mimetypes for non-images
        # (only served via download=1 anyway).
        is_pdf = name.lower().endswith(".pdf")
        media_type = (
            "application/pdf"
            if is_pdf
            else preview_image_mime(name) or mimetypes.guess_type(name)[0]
        )
        # Inline only browser-renderable images (by extension) plus PDF, whose
        # in-browser viewer is sandboxed away from the page's origin (no DOM or
        # cookie access — the Files panel embeds it for preview). SVG is excluded
        # (it can carry scripts — a crafted upload would be a stored-XSS vector);
        # it stays reachable via download=1. See lovia/web/media.py.
        inline_ok = is_preview_image(name) or is_pdf
        if not download and not inline_ok:
            raise WebError(
                415, "unsupported_file_type", "inline preview is images and PDFs only"
            )
        # `no-cache` = cache but revalidate: the viewer re-opens the same URL
        # constantly (and re-renders after every agent edit), so unchanged
        # files answer 304 from the ETag instead of re-sending bytes — and
        # changed files show fresh without any query-string busting.
        # `nosniff` stops a browser from MIME-sniffing a mislabeled file (e.g. a
        # non-image named .png) and rendering it as HTML — defense in depth for
        # serving user-uploaded bytes inline, on top of the images-only gate.
        return media_type or "application/octet-stream", {
            "cache-control": "no-cache",
            "x-content-type-options": "nosniff",
        }

    def _raw_local(
        cfg: LocalWorkspace, path: str, request: Request, download: bool
    ) -> Response:
        """``raw`` for a file on this machine: straight off the disk."""
        # Same normalization LocalWorkspaceSession applies to its root.
        target = resolve_path(Path(cfg.root).expanduser().resolve(), path).abs
        if not target.is_file():
            raise WebError(404, "file_not_found", "no such file")
        try:
            stat_result = target.stat()
        except OSError as exc:  # vanished between the is_file check and here
            raise WebError(404, "file_not_found", "no such file") from exc
        media_type, headers = _raw_head(cfg, target.name, stat_result.st_size, download)
        response = FileResponse(
            target,
            media_type=media_type,
            stat_result=stat_result,
            filename=target.name if download else None,
            content_disposition_type="attachment" if download else "inline",
            headers=headers,
        )
        etag = response.headers.get("etag")
        if etag and _not_modified(request, etag):
            return Response(status_code=304, headers={"etag": etag, **headers})
        return response

    # ---- background processes (chat-scoped) --------------------------------
    # Keyed by web session id, not agent: processes belong to the chat's live
    # workspace session (lovia/web/workspaces.py). Listing is passive; kill is
    # the one user-side control — "stop the dev server without asking the
    # model". The model's next status reminder shows the process as killed.

    async def _process_list(session_id: str) -> list[ProcessInfo]:
        session = deps.workspaces.get(session_id)
        if session is None:
            return []
        statuses = session.background_processes()
        if inspect.isawaitable(statuses):  # a remote session asks its host
            statuses = await statuses
        return [
            ProcessInfo(
                process_id=s.process_id,
                command=s.command,
                status=s.status,
                exit_code=s.exit_code,
            )
            for s in statuses
        ]

    @router.get(
        "/api/sessions/{session_id}/processes", response_model=list[ProcessInfo]
    )
    async def list_processes(session_id: str) -> list[ProcessInfo]:
        """Background processes of the chat's workspace session.

        ``[]`` when the chat has no live session (nothing ran yet, or the
        server restarted — processes never survive a restart).
        """
        return await _process_list(session_id)

    @router.post(
        "/api/sessions/{session_id}/processes/{process_id}/kill",
        response_model=list[ProcessInfo],
    )
    async def kill_process(session_id: str, process_id: str) -> list[ProcessInfo]:
        """Kill one background process; returns the refreshed process list."""
        session = deps.workspaces.get(session_id)
        if session is None:
            raise WebError(404, "process_not_found", "no live workspace session")
        try:
            await session.kill_process(process_id)
        except WorkspaceError as exc:  # unknown id (or a closed session)
            raise WebError.from_exc(404, "process_not_found", exc) from exc
        return await _process_list(session_id)

    @router.post("/api/workspace/upload", response_model=UploadedFile)
    async def upload_file(
        file: UploadFile = File(...),
        agent: str | None = Query(None),
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
    ) -> UploadedFile:
        """Save a composer/Files-panel upload under the workspace ``uploads/``.

        Requires a workspace the panel serves (same 404 gate). The file's
        extension is checked against an allowlist (``LOVIA_UPLOAD_ALLOWED_EXT``)
        and its size against a cap (``LOVIA_MAX_UPLOAD_MB``) enforced while
        reading, so a disallowed or huge upload is rejected without exhausting
        memory. The bytes are then written to a collision-free name, immediately
        servable via ``/api/workspace/raw`` and referenced from chat by path.

        Written through a session of its own, not the chat's: an upload is
        the user's act, so a chat in a read-only mode takes one too — but
        only ever under the root (an ``uploads`` that links out of it is
        refused).
        """
        cfg = await require_cfg(agent, chat, root)
        name = _safe_upload_name(file.filename or "upload")
        allowed = _allowed_upload_ext()
        max_bytes = _max_upload_bytes()
        ext = Path(name).suffix.lower().lstrip(".")
        try:
            if allowed is not None and ext and ext not in allowed:
                raise WebError(
                    415, "unsupported_file_type", f"file type '.{ext}' is not allowed"
                )
            data = bytearray()
            while chunk := await file.read(1 << 20):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise WebError(413, "file_too_large", "file too large")
            if not data:
                raise WebError(422, "invalid_request", "empty file")
        finally:
            await file.close()
        async with _view(cfg, write=True) as session:
            saved, name = await _write_upload(session, name, bytes(data))
        # Type comes from the SAVED extension, never the client's Content-Type;
        # kind="image" is the browser-previewable set (SVG excluded — it can
        # carry scripts), so the composer previews only what is safe to render.
        mime = (
            preview_image_mime(name)
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        return UploadedFile(
            path=saved,
            name=name,
            mime=mime,
            kind="image" if is_preview_image(name) else "file",
            size=len(data),
        )

    return router
