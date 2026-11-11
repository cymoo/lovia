"""Fold composer attachments into the user turn the runner sees.

``POST /api/workspace/upload`` has already written each attachment under the
workspace ``uploads/`` dir; here we assemble one message's text + attachment
references into a run's input, or into a steer for a live run. The rules
mirror issue #107's two stages:

* Every attachment is surfaced as a **workspace path** the agent can reach with
  its own file tools (or ``describe_image``) — this works with any model.
* Images additionally go **inline** as :class:`~lovia.ImagePart` only when the
  model can see them (:func:`~lovia.providers.supports_vision`).

The client's ``path`` is never trusted: each is looked up through a readonly
session on the chat's workspace (:func:`~lovia.web.workspaces.open_view`) —
on its host, for a remote one — and dropped if it escapes the root or
doesn't exist.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from ..agent import Agent
from ..exceptions import UserError
from ..messages import Message, user
from ..parts import ContentPart, ImagePart, TextPart
from ..providers import supports_vision
from ..workspace import WorkspaceDisconnectedError, WorkspaceError
from .errors import WebError
from .media import model_image_mime
from .schemas import Attachment, ChatRequest
from .workspaces import files_config, open_view

log = logging.getLogger(__name__)


async def user_content(
    message: str, attachments: list[Attachment] | None, agent: Agent[Any]
) -> str | list[ContentPart]:
    """One user message's content: plain text, or multimodal parts.

    Returns ``message`` unchanged when there are no usable attachments, so
    the common text-only path is untouched. Otherwise returns the text, any
    inlined images, and a note pointing at every attachment's workspace path.

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

    try:
        session = await open_view(cfg)
    except UserError:  # the root is gone: nothing in it to attach
        return message
    except WorkspaceError as exc:
        raise WebError.from_exc(502, "workspace_unreachable", exc) from exc
    rels: list[str] = []
    try:
        for att in attachments:
            try:
                # Outside the root is a deny under the view's policy, so
                # what gets past this is inside it.
                entry = await session.stat(att.path)
            except WorkspaceDisconnectedError:
                raise
            except (OSError, UserError, WorkspaceError) as exc:
                log.warning("dropping attachment %r: %s", att.path, exc)
                continue
            if entry.is_dir:
                log.warning("dropping attachment %r: a directory", att.path)
                continue
            rels.append(entry.path)
            # Whether to inline is decided from the saved file's extension,
            # not the client-supplied kind/mime — the file on disk is the
            # source of truth.
            image_mime = model_image_mime(entry.path) if can_see else None
            if image_mime is None:
                continue
            try:
                image = await session.read_bytes(
                    att.path, max_bytes=cfg.limits.max_file_read_bytes
                )
            except WorkspaceDisconnectedError:
                raise
            except (OSError, UserError, WorkspaceError) as exc:
                # Still attached by path: the note below names it.
                log.warning("could not inline image %s: %s", entry.path, exc)
                continue
            parts.append(
                ImagePart(
                    data=base64.b64encode(image.data).decode("ascii"),
                    mime_type=image_mime,
                )
            )
    except WorkspaceDisconnectedError as exc:
        raise WebError.from_exc(502, "workspace_unreachable", exc) from exc
    finally:
        await session.close()

    if not rels:
        return message  # every attachment was dropped → plain text

    # A path note so the agent can reach every file with its own tools (and any
    # image a non-vision model can't see). Separated from the user's own text
    # with a blank line so history replay doesn't run the two together.
    lead = "\n\n" if message.strip() else ""
    parts.append(TextPart(f"{lead}{_attachment_note(rels)}"))
    return parts


async def build_user_input(req: ChatRequest, agent: Agent[Any]) -> str | list[Message]:
    """The runner input for one turn: a plain string, or one multimodal message."""
    content = await user_content(req.message, req.attachments, agent)
    return content if isinstance(content, str) else [user(content)]


def _attachment_note(rels: list[str]) -> str:
    """A one-line note telling the model where the attachments live on disk."""
    return (
        "[Attached in the workspace: "
        + ", ".join(rels)
        + ". Read with your file tools when relevant.]"
    )
