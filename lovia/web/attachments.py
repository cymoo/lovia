"""Fold composer attachments into the user turn the runner sees.

``POST /api/workspace/upload`` has already written each attachment under the
workspace ``uploads/`` dir; here we assemble one message's text + attachment
references into a run's input, or into a steer for a live run. The rules
mirror issue #107's two stages:

* Every attachment is surfaced as a **workspace path** the agent can reach with
  its own file tools (or ``describe_image``) — this works with any model.
* Images additionally go **inline** as :class:`~lovia.ImagePart` only when the
  model can see them (:func:`~lovia.providers.supports_vision`).

The client's ``path`` is never trusted: each is re-resolved against the
workspace root and dropped if it escapes or doesn't exist.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..agent import Agent
from ..messages import Message, user
from ..parts import ContentPart, ImagePart, TextPart
from ..providers import supports_vision
from ..workspace import LocalWorkspace
from ..workspace.paths import resolve_path
from .media import model_image_mime
from .schemas import Attachment, ChatRequest

log = logging.getLogger(__name__)


def _workspace_root(agent: Agent[Any]) -> Path | None:
    """The agent's local-workspace root, or None (mirrors ``workspace_cfg``)."""
    ws = agent.workspace
    cfg = getattr(ws, "workspace", ws)
    if isinstance(cfg, LocalWorkspace):
        return Path(cfg.root).expanduser().resolve()
    return None


def user_content(
    message: str, attachments: list[Attachment] | None, agent: Agent[Any]
) -> str | list[ContentPart]:
    """One user message's content: plain text, or multimodal parts.

    Returns ``message`` unchanged when there are no usable attachments, so
    the common text-only path is untouched. Otherwise returns the text, any
    inlined images, and a note pointing at every attachment's workspace path.
    """
    attachments = attachments or []
    root = _workspace_root(agent)
    if not attachments or root is None:
        return message

    can_see = supports_vision(getattr(agent, "model", None))
    parts: list[ContentPart] = []
    if message.strip():
        parts.append(TextPart(message))

    rels: list[str] = []
    for att in attachments:
        resolved = resolve_path(root, att.path)
        if not resolved.inside or not resolved.abs.is_file():
            log.warning(
                "dropping attachment outside workspace or missing: %r", att.path
            )
            continue
        rels.append(resolved.rel or att.path)
        # Whether to inline is decided from the saved file's extension, not the
        # client-supplied kind/mime — the file on disk is the source of truth.
        image_mime = model_image_mime(resolved.abs) if can_see else None
        if image_mime:
            try:
                parts.append(ImagePart.from_path(resolved.abs, mime_type=image_mime))
            except (OSError, ValueError) as exc:  # pragma: no cover - defensive
                log.warning("could not inline image %s: %s", resolved.rel, exc)

    if not rels:
        return message  # every attachment was dropped → plain text

    # A path note so the agent can reach every file with its own tools (and any
    # image a non-vision model can't see). Separated from the user's own text
    # with a blank line so history replay doesn't run the two together.
    lead = "\n\n" if message.strip() else ""
    parts.append(TextPart(f"{lead}{_attachment_note(rels)}"))
    return parts


def build_user_input(req: ChatRequest, agent: Agent[Any]) -> str | list[Message]:
    """The runner input for one turn: a plain string, or one multimodal message."""
    content = user_content(req.message, req.attachments, agent)
    return content if isinstance(content, str) else [user(content)]


def _attachment_note(rels: list[str]) -> str:
    """A one-line note telling the model where the attachments live on disk."""
    return (
        "[Attached in the workspace: "
        + ", ".join(rels)
        + ". Read with your file tools when relevant.]"
    )
