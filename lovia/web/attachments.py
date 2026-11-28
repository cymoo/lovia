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
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..agent import Agent
from ..exceptions import UserError
from ..messages import Message, user
from ..parts import ContentPart, ImagePart, TextPart, sniff_image_mime
from ..providers import supports_vision
from ..workspace import (
    RemoteWorkspace,
    WorkspaceDisconnectedError,
    WorkspaceError,
    WorkspaceLimits,
)
from ..workspace.protocol import WorkspaceSession
from ..workspace.tools import VIEW_IMAGE_MAX_BYTES
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

    Collision-free on any filesystem the folder is copied to, not just this
    one: a name is taken when one already there differs only in case or
    Unicode form — ``A.png`` and ``a.png`` are one file on a default macOS
    or Windows host, whatever this one says. Each candidate is created
    exclusively, so two concurrent uploads of one name can't clobber each
    other — the loser moves on to the next suffix.
    """
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    taken = {_fold(p.name) for p in folder.iterdir()}
    for candidate in _candidates(name):
        if _fold(candidate) in taken:
            continue
        try:
            fd = os.open(folder / candidate, flags, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        return candidate
    raise AssertionError("unreachable: the candidates never run out")


def _fold(name: str) -> str:
    """``name`` as a case-insensitive, normalizing filesystem compares it."""
    return unicodedata.normalize("NFC", name).casefold()


def sees_images(agent: Agent[Any]) -> bool:
    """Whether ``agent``'s model takes images inline — a ``"vendor:model"``
    string resolved to its provider first, as a run would (the string itself
    says nothing). An agent whose model can't be resolved here sees none."""
    try:
        return supports_vision(agent.resolve_provider())
    except Exception:  # noqa: BLE001 — no model, a vendor not installed: no
        return False


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

    An agent without a workspace has no file tools: it gets its images —
    inline when its model sees them — and nothing else.

    A workspace that can't be reached at all (a remote host that is down) is
    a 502, not a message sent without what it was shown.
    """
    attachments = attachments or []
    if not attachments:
        return message
    cfg = files_config(agent)

    can_see = sees_images(agent)
    parts: list[ContentPart] = []
    if message.strip():
        parts.append(TextPart(message))
    named: list[str] = []

    stored = any(att.path is None for att in attachments)
    linked = cfg is not None and any(att.path is not None for att in attachments)
    host_folder: str | None = None
    uncopied: set[str] = set()
    if stored and folder is not None:
        # In use now: a draft idle past the sweep's age is about to become a
        # chat, and the sweep re-reads the mtime before it removes anything.
        await asyncio.to_thread(_touch, folder)
        if isinstance(cfg, RemoteWorkspace):
            host_folder, uncopied = await _staged(cfg, folder)
    session: WorkspaceSession | None = None
    if linked and cfg is not None:
        try:
            session = await open_view(cfg)
        except UserError:  # the root is gone: what lives in it can't be had
            session = None
        except WorkspaceError as exc:
            raise WebError.from_exc(502, "workspace_unreachable", exc) from exc
    limits = cfg.limits if cfg is not None else WorkspaceLimits()
    # An image larger than a provider takes is attached by path only.
    max_bytes = min(limits.max_file_read_bytes, VIEW_IMAGE_MAX_BYTES)
    try:
        for att in attachments:
            image: bytes | None = None
            if att.path is None:
                file = await _stored(att, folder)
                if file is None:
                    continue
                if cfg is None and model_image_mime(file.name) is None:
                    log.warning(
                        "dropping attachment %r: no workspace to read it", file.name
                    )
                    continue
                if file.name in uncopied:
                    # Its host copy is missing or stale: naming it would
                    # point the agent at what the user did not send.
                    log.warning("dropping attachment %r: not on the host", file.name)
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
            # Inline only what a provider takes: an image type by its bytes
            # (a mislabeled one is refused), within the per-image cap.
            if not can_see or image is None or len(image) > max_bytes:
                continue  # still attached by path: the note names it
            mime = sniff_image_mime(image)
            if mime is None:
                continue
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
    parts.append(TextPart(f"{lead}{_attachment_note(named, tools=cfg is not None)}"))
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


async def stage_on_host(cfg: RemoteWorkspace, folder: Path) -> tuple[str, set[str]]:
    """Put the chat's attachments on ``cfg``'s host — each one missing there,
    or whose size or mtime differ from the original's (a copy something
    changed: a put stamps the original's mtime) — and return the folder the
    host keeps them in, and the names whose copy couldn't be put (their
    original unreadable here): there is no good copy of those to name.

    The host's copies are a cache of ``folder``: a host that lost them (a
    container rebuilt, its temp dir cleaned) gets them back here, before a
    run and on every send. A host that won't keep copies (its folder isn't
    private to its account) is a :class:`PermissionError` saying why.
    """
    there, copies, refused = await cfg.host.attachments_list(folder.name)
    if refused is not None:
        raise PermissionError(refused)  # the host's own words, errno and path
    return there, await _put_stale(cfg, folder, copies)


async def _put_stale(
    cfg: RemoteWorkspace, folder: Path, copies: dict[str, tuple[int, int]]
) -> set[str]:
    """Put each original in ``folder`` whose host copy is missing or differs;
    return the names left without a good copy. Only the host's errors come
    out of here: an original that can't be read on this side is left
    behind, named in what comes back."""
    uncopied: set[str] = set()
    local = await asyncio.to_thread(_stats, folder)
    for name, (size, mtime) in local.items():
        if copies.get(name) == (size, int(mtime)):
            continue
        try:
            data = await asyncio.to_thread((folder / name).read_bytes)
        except OSError as exc:
            log.warning("not copying attachment %r to %s: %s", name, cfg.host.name, exc)
            uncopied.add(name)
            continue
        await cfg.host.attachments_put(folder.name, name, data, mtime)
    return uncopied


async def _staged(cfg: RemoteWorkspace, folder: Path) -> tuple[str, set[str]]:
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
    there first, whatever the host lost. None for an id that names no folder.

    A remote run always gets its folder, guarded: a host that won't keep
    copies, or a put that fails, leaves the copies short (logged, tried
    again next run) — never what is already there writable. A host out of
    reach fails the run here, as opening its session would next.
    """
    folder = await store.attachment_folder(session_id)
    cfg = files_config(agent)
    if folder is None or not isinstance(cfg, RemoteWorkspace):
        return folder
    there, copies, refused = await cfg.host.attachments_list(folder.name)
    if refused is not None:
        log.warning("attachments not copied to %s: %s", cfg.host.name, refused)
    else:
        try:
            await _put_stale(cfg, folder, copies)
        except (OSError, WorkspaceError) as exc:
            log.warning("attachments not copied to %s: %s", cfg.host.name, exc)
    return HostFolder(there)


def _stats(folder: Path) -> dict[str, tuple[int, float]]:
    """Each attachment in ``folder`` by name, with its size and mtime; none
    for a folder not made yet."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return {}
    stats: dict[str, tuple[int, float]] = {}
    for e in entries:
        try:
            # Not the store's own (its owner mark): only what was uploaded.
            if e.is_file(follow_symlinks=False) and is_attachment_name(e.name):
                st = e.stat(follow_symlinks=False)
                stats[e.name] = (st.st_size, st.st_mtime)
        except OSError:  # gone meanwhile
            continue
    return stats


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


def _attachment_note(paths: list[str], *, tools: bool) -> str:
    """A note telling the model where the attachments are, one path a line
    (a path may hold ``", "``; none holds a newline) — and, when it has
    them, to use its file tools. The page splits it back out of history
    (``splitAttachmentNote`` in ``static/js/attachment-note.js``) — keep the
    two in sync."""
    lines = "\n".join(paths)
    hint = "\nRead with your file tools when relevant." if tools else ""
    return f"[Attached:\n{lines}{hint}]"
