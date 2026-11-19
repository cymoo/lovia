"""Attachment routes: a chat's composer uploads, saved in its own folder.

An upload goes to the chat's attachment folder in the store
(:meth:`~lovia.web.store.ChatStore.attachment_folder`), never into a
workspace — so the chat needn't exist yet: a draft uploads under the id it
will send with, and the chat that send creates already owns the folder. The
page only ever sees file names.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

try:
    from fastapi import APIRouter, File, Query, Request, UploadFile
    from fastapi.responses import Response
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..attachments import (
    allowed_upload_ext,
    is_attachment_name,
    max_upload_bytes,
    safe_upload_name,
    save_attachment,
)
from ..errors import WebError
from ..media import is_preview_image, model_image_mime, preview_image_mime
from ...workspace.workspace import WorkspaceSessionBinding
from ..schemas import UploadedFile
from ..workspaces import files_config
from .deps import RouterDeps
from .workspace import serve_local

_NO_CAP = 1 << 62


def build_attachments_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    async def folder_of(session_id: str) -> Path:
        folder = await deps.store.attachment_folder(session_id)
        if folder is None:
            raise WebError(
                422,
                "invalid_request",
                "this chat id can't hold attachments",
                hint="attachments go with a 32-character lowercase hex chat id "
                "(a uuid4's hex); a new chat's client mints one",
            )
        return folder

    @router.post("/api/sessions/{session_id}/attachments", response_model=UploadedFile)
    async def upload_attachment(
        session_id: str,
        file: UploadFile = File(...),
        agent: str | None = Query(None),
    ) -> UploadedFile:
        """Save one file into the chat's attachment folder.

        ``session_id`` may name a chat that doesn't exist yet: a draft
        uploads under the id it then sends with, naming its ``agent``. An
        agent without a workspace has no file tools to read with: it takes
        images only, which its model sees inline. The extension is checked
        against an allowlist (``LOVIA_UPLOAD_ALLOWED_EXT``) and the size
        against a cap (``LOVIA_MAX_UPLOAD_MB``) enforced while reading, so a
        disallowed or huge upload is rejected without exhausting memory. The
        bytes are written at a collision-free name, returned for the message
        to name.
        """
        meta = await deps.store.get(session_id)
        picked = deps.pick(deps.agent_of(meta) if meta else agent)
        name = safe_upload_name(file.filename or "upload")
        if files_config(picked) is None:
            if model_image_mime(name) is None:
                raise WebError(
                    415,
                    "unsupported_file_type",
                    "this agent has no workspace: it takes images only",
                    hint="png, jpeg, gif or webp; other files need an agent "
                    "with a workspace to read them",
                )
        elif isinstance(picked.workspace, WorkspaceSessionBinding):
            # One session the caller opened serves every chat of the agent:
            # a chat's folder can't be made readable (and never writable)
            # there without opening it to every other chat as well.
            raise WebError(
                404,
                "feature_unavailable",
                "this agent's workspace session is the caller's own, shared by "
                "every chat: it can't keep a chat's attachments read-only",
                hint="give the agent its workspace config (a LocalWorkspace), "
                "not a bound session: lovia web binds one session per chat",
            )
        folder = await folder_of(session_id)
        allowed = allowed_upload_ext()
        max_bytes = max_upload_bytes()
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
        try:
            name = await asyncio.to_thread(save_attachment, folder, name, bytes(data))
        except OSError as exc:  # a full disk, a folder we can't write
            raise WebError.from_exc(500, "attachment_io_error", exc) from exc
        # Type comes from the SAVED extension, never the client's Content-Type;
        # kind="image" is the browser-previewable set (SVG excluded — it can
        # carry scripts), so the composer previews only what is safe to render.
        mime = (
            preview_image_mime(name)
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        return UploadedFile(
            name=name,
            mime=mime,
            kind="image" if is_preview_image(name) else "file",
            size=len(data),
        )

    @router.get("/api/sessions/{session_id}/attachments/{name}")
    async def get_attachment(
        session_id: str, name: str, request: Request, download: bool = Query(False)
    ) -> Response:
        """One attachment's bytes, by the name its upload returned: inline for
        images and PDFs, as a download for any file with ``download=1`` —
        the same rules as ``/api/workspace/raw``. A fork reads its source's
        (they share the folder); a deleted chat's are gone."""
        if not is_attachment_name(name):
            raise WebError(404, "file_not_found", "no such attachment")
        folder = await folder_of(session_id)
        # No size cap here: the upload's own let the file in.
        return serve_local(folder / name, request, download, _NO_CAP)

    return router
