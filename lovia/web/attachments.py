"""Composer attachments: saved with their chat, folded into its user turn.

An attachment is the chat's input, not the project's content: its bytes live
in the chat's folder under the store's ``attachments_dir``
(:meth:`~lovia.web.store.ChatStore.attachment_folder`), never in the
workspace, and go with the last chat that uses the folder. Design:
``docs/design/web-attachments.md``.

Here we save an upload into a folder, and assemble one message's text and
attachments into a run's input or a steer:

* Every attachment is named in a note the agent can act on — the file's
  absolute path, which the chat's runs may read and never write
  (:func:`~lovia.web.workspaces.attachment_policy`). A chat on a remote host
  works with a copy the host keeps in a temp folder of its account's own,
  outside the workspace too (:func:`stage_on_host`), and the note names that.
* Images also go **inline** as :class:`~lovia.ImagePart` when the model can
  see them (:func:`~lovia.providers.supports_vision`).
* A workspace file attached from the Files panel (by ``path``) is referenced
  where it is. That path is never trusted: it is looked up through a readonly
  view of the chat's workspace (:func:`~lovia.web.workspaces.open_view`) and
  dropped if it escapes the root or doesn't exist.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agent import Agent
from ..exceptions import UserError
from ..messages import Message, user
from ..parts import ContentPart, ImagePart, TextPart
from ..providers import supports_vision
from ..workspace import (
    RemoteWorkspace,
    WorkspaceDisconnectedError,
    WorkspaceError,
)
from ..workspace.protocol import WorkspaceSession
from .errors import WebError
from .media import model_image_mime
from .schemas import Attachment, ChatRequest
from .workspaces import AttachmentFolder, HostFolder, files_config, open_view

if TYPE_CHECKING:
    from .store import ChatStore

log = logging.getLogger(__name__)

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
# The longest name an upload is saved under.
_MAX_NAME = 120
# Names Windows gives to devices, whatever the extension (``con.png``).
_WINDOWS_DEVICES = frozenset(
    ["con", "prn", "aux", "nul"]
    + [f"{d}{i}" for d in ("com", "lpt") for i in range(1, 10)]
)


def max_upload_bytes() -> int:
    """Upload size cap in bytes (env ``LOVIA_MAX_UPLOAD_MB``, default 25)."""
    raw = os.environ.get("LOVIA_MAX_UPLOAD_MB")
    mb = _DEFAULT_MAX_UPLOAD_MB
    if raw:
        try:
            mb = max(1, int(raw))
        except ValueError:
            pass
    return mb * 1024 * 1024


def allowed_upload_ext() -> frozenset[str] | None:
    """Allowed upload extensions (env ``LOVIA_UPLOAD_ALLOWED_EXT``); ``None``
    means "any" (the var set to ``*`` or empty)."""
    raw = os.environ.get("LOVIA_UPLOAD_ALLOWED_EXT")
    if raw is None:
        return _DEFAULT_ALLOWED_UPLOAD_EXT
    raw = raw.strip()
    if not raw or raw == "*":
        return None
    return frozenset(e.lower().lstrip(".") for e in re.split(r"[,\s]+", raw) if e)


def safe_upload_name(raw: str) -> str:
    """A filesystem-safe basename: strip any directory, keep readable chars.

    ``\\w`` is Unicode-aware, so CJK filenames survive; separators and control
    characters collapse to ``_``. The result is always a plain basename, and
    never a Windows device name. Idempotent, so a name the client echoes back
    is checked by :func:`is_attachment_name`.
    """
    base = Path(raw).name.strip()
    base = re.sub(r"[^\w.\- ]+", "_", base).strip(". ") or "upload"
    if base.split(".", 1)[0].rstrip().lower() in _WINDOWS_DEVICES:
        base = f"_{base}"
    return _fit(base)


def _fit(name: str) -> str:
    """``name`` cut to the cap: the stem gives way, the extension stays (a
    "report.pdf" is still a PDF), and the cut never leaves an end the
    cleaning above strips — or the name would not read back as itself."""
    if len(name) <= _MAX_NAME:
        return name
    stem, suffix = _split(name)
    return (stem[: _MAX_NAME - len(suffix)].rstrip(". ") or "upload") + suffix


def _split(name: str) -> tuple[str, str]:
    """``name``'s stem and extension, as a cut or a dedup suffix sees them:
    an "extension" longer than half the cap is part of the name."""
    stem, suffix = os.path.splitext(name)
    return (name, "") if len(suffix) > _MAX_NAME // 2 else (stem, suffix)


def is_attachment_name(name: str) -> bool:
    """Whether ``name`` is one :func:`safe_upload_name` could have produced:
    a plain file name, nothing that walks out of its folder."""
    return bool(name) and safe_upload_name(name) == name


def _candidates(name: str) -> Iterator[str]:
    """``name``, then ``stem-1.ext``, ``stem-2.ext``, … — tried in order until
    one is free (each is created exclusively by its caller). The stem gives
    way to the suffix, so every candidate stays a name
    :func:`is_attachment_name` accepts: one past the cap could be saved but
    never served or sent."""
    yield name
    stem, suffix = _split(name)
    for i in itertools.count(1):
        tail = f"-{i}{suffix}"
        yield f"{stem[: max(0, _MAX_NAME - len(tail))]}{tail}"


def save_attachment(folder: Path, name: str, data: bytes) -> str:
    """Write ``data`` into ``folder`` at a collision-free name; return it.

    Each candidate is created exclusively, so two concurrent uploads that
    settle on one name can't clobber each other — the loser moves on to the
    next suffix. On a case-insensitive filesystem ``A.png`` takes ``a.png``'s
    place the same way.
    """
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for candidate in _candidates(name):
        try:
            fd = os.open(folder / candidate, flags, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        return candidate
    raise AssertionError("unreachable: the candidates never run out")


async def user_content(
    message: str,
    attachments: list[Attachment] | None,
    agent: Agent[Any],
    folder: Path | None,
) -> str | list[ContentPart]:
    """One user message's content: plain text, or multimodal parts.

    ``folder`` is the chat's attachment folder, where a stored attachment (one
    named, not given a ``path``) is looked up. Returns ``message`` unchanged
    when there are no usable attachments, so the common text-only path is
    untouched. Otherwise returns the text, any inlined images, and a note
    naming every attachment.

    A workspace that can't be reached at all (a remote host that is down) is
    a 502, not a message sent without what it was shown.
    """
    attachments = attachments or []
    cfg = files_config(agent)
    if not attachments or cfg is None:
        return message

    can_see = supports_vision(getattr(agent, "model", None))
    parts: list[ContentPart] = []
    if message.strip():
        parts.append(TextPart(message))
    named: list[str] = []

    stored = any(att.path is None for att in attachments)
    linked = any(att.path is not None for att in attachments)
    host_folder: str | None = None
    if stored and folder is not None:
        # In use now: a draft idle past the sweep's age is about to become a
        # chat, and the sweep re-reads the mtime before it removes anything.
        await asyncio.to_thread(_touch, folder)
        if isinstance(cfg, RemoteWorkspace):
            host_folder = await _staged(cfg, folder)
    session: WorkspaceSession | None = None
    if linked:
        try:
            session = await open_view(cfg)
        except UserError:  # the root is gone: what lives in it can't be had
            session = None
        except WorkspaceError as exc:
            raise WebError.from_exc(502, "workspace_unreachable", exc) from exc
    max_bytes = cfg.limits.max_file_read_bytes
    try:
        for att in attachments:
            image: bytes | None = None
            if att.path is None:
                file = await _stored(att, folder)
                if file is None:
                    continue
                # A remote chat's tools find the host's copy.
                where = (
                    str(file) if host_folder is None else f"{host_folder}/{file.name}"
                )
                if can_see and model_image_mime(file.name):
                    image = await _read_local(file, max_bytes)
            elif session is not None:
                found = await _linked(att, session, max_bytes)
                if found is None:
                    continue
                where, image = found
            else:
                continue
            named.append(where)
            mime = model_image_mime(where) if can_see else None
            if mime is None or image is None or len(image) > max_bytes:
                continue  # still attached by path: the note names it
            parts.append(
                ImagePart(data=base64.b64encode(image).decode("ascii"), mime_type=mime)
            )
    except WorkspaceDisconnectedError as exc:
        raise WebError.from_exc(502, "workspace_unreachable", exc) from exc
    finally:
        if session is not None:
            await session.close()

    if not named:
        return message  # every attachment was dropped → plain text

    # A path note so the agent can reach every file with its own tools (and any
    # image a non-vision model can't see). Separated from the user's own text
    # with a blank line so history replay doesn't run the two together.
    lead = "\n\n" if message.strip() else ""
    parts.append(TextPart(f"{lead}{_attachment_note(named)}"))
    return parts


async def _stored(att: Attachment, folder: Path | None) -> Path | None:
    """A stored attachment's file in the chat's folder, absolute; None to
    drop it."""
    name = att.name or ""
    if folder is None or not is_attachment_name(name):
        log.warning("dropping attachment %r: not a stored attachment", name)
        return None
    file = folder.resolve() / name
    if not await asyncio.to_thread(file.is_file):
        log.warning("dropping attachment %r: not in the chat's folder", name)
        return None
    return file


async def stage_on_host(cfg: RemoteWorkspace, folder: Path) -> str:
    """Put the chat's attachments on ``cfg``'s host — each one missing there,
    or of another size (a copy something changed) — and return the folder
    the host keeps them in.

    The host's copies are a cache of ``folder``: a host that lost them (a
    container rebuilt, its temp dir cleaned) gets them back here, before a
    run and on every send.
    """
    local = await asyncio.to_thread(_sizes, folder)
    there, sizes = await cfg.host.attachments_list(folder.name)
    for name, size in local.items():
        if sizes.get(name) != size:
            data = await asyncio.to_thread((folder / name).read_bytes)
            await cfg.host.attachments_put(folder.name, name, data)
    return there


async def _staged(cfg: RemoteWorkspace, folder: Path) -> str:
    """:func:`stage_on_host` for a message: what goes wrong is its answer —
    the host's own "no" with what it said, a host out of reach a 502."""
    try:
        return await stage_on_host(cfg, folder)
    except PermissionError as exc:
        raise WebError.from_exc(403, "path_denied", exc) from exc
    except OSError as exc:  # a full disk there
        raise WebError.from_exc(500, "workspace_io_error", exc) from exc
    except WorkspaceError as exc:
        raise WebError.from_exc(502, "workspace_unreachable", exc) from exc


async def attachments_for_run(
    store: ChatStore, session_id: str, agent: Agent[Any]
) -> AttachmentFolder | None:
    """The attachment folder a run of ``session_id`` reads (and never
    writes): the chat's own, or for a remote chat the host's copy — put
    there first, whatever the host lost. None when there is none to guard:
    an id that names no folder, or a host that would not take the copies
    (the run then can't reach them; the next one tries again)."""
    folder = await store.attachment_folder(session_id)
    cfg = files_config(agent)
    if folder is None or not isinstance(cfg, RemoteWorkspace):
        return folder
    try:
        return HostFolder(await stage_on_host(cfg, folder))
    except (OSError, WorkspaceError) as exc:
        log.warning("attachments not on %s for %s: %s", cfg.host.name, session_id, exc)
        return None


def _sizes(folder: Path) -> dict[str, int]:
    """Each attachment in ``folder`` by name, with its size; none for a
    folder not made yet."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return {}
    return {
        e.name: e.stat(follow_symlinks=False).st_size
        for e in entries
        if e.is_file(follow_symlinks=False)
    }


async def _linked(
    att: Attachment, session: WorkspaceSession, max_bytes: int
) -> tuple[str, bytes | None] | None:
    """A workspace file attached by path: its path, as the view resolves it,
    and its bytes when it is an image; None to drop it."""
    path = att.path or ""
    try:
        # Outside the root is a deny under the view's policy, so what gets
        # past this is inside it.
        entry = await session.stat(path)
    except WorkspaceDisconnectedError:
        raise
    except (OSError, UserError, WorkspaceError) as exc:
        log.warning("dropping attachment %r: %s", path, exc)
        return None
    if entry.is_dir:
        log.warning("dropping attachment %r: a directory", path)
        return None
    if "\n" in entry.path or "\r" in entry.path:  # the note is one path a line
        log.warning("dropping attachment %r: a line break in its name", path)
        return None
    # Whether to inline is decided from the file's extension, not the
    # client-supplied kind/mime — the file on disk is the source of truth.
    if model_image_mime(entry.path) is None:
        return entry.path, None
    try:
        image = await session.read_bytes(path, max_bytes=max_bytes)
    except WorkspaceDisconnectedError:
        raise
    except (OSError, UserError, WorkspaceError) as exc:
        log.warning("could not inline image %s: %s", entry.path, exc)
        return entry.path, None
    return entry.path, image.data


def _touch(folder: Path) -> None:
    with contextlib.suppress(OSError):  # no folder: nothing for a sweep to take
        os.utime(folder)


async def _read_local(file: Path, max_bytes: int) -> bytes | None:
    """``file``'s bytes to inline, or None when it is too large to."""
    try:
        size = (await asyncio.to_thread(file.stat)).st_size
        if size > max_bytes:
            return None
        return await asyncio.to_thread(file.read_bytes)
    except OSError as exc:
        log.warning("could not inline image %s: %s", file, exc)
        return None


async def build_user_input(
    req: ChatRequest, agent: Agent[Any], folder: Path | None
) -> str | list[Message]:
    """The runner input for one turn: a plain string, or one multimodal message."""
    content = await user_content(req.message, req.attachments, agent, folder)
    return content if isinstance(content, str) else [user(content)]


def _attachment_note(paths: list[str]) -> str:
    """A note telling the model where the attachments are, one path a line
    (a path may hold ``", "``; none holds a newline). The page splits it
    back out of history (``splitAttachmentNote`` in ``static/js/chat.js``)
    — keep the two in sync."""
    lines = "\n".join(paths)
    return f"[Attached:\n{lines}\nRead with your file tools when relevant.]"
