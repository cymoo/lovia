"""Fold composer attachments into the user turn the runner sees.

``POST /api/workspace/upload`` has already written each attachment under the
workspace ``uploads/`` dir; here we assemble one turn's text + attachment
references into runner input. The rules mirror issue #107's two stages:

* Every attachment is surfaced as a **workspace path** the agent can reach with
  its own file tools (or ``see_image``) — this works with any model.
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
from .schemas import ChatRequest

log = logging.getLogger(__name__)

# Image mime types ImagePart can inline (matches lovia.web.vision._IMAGE_MIME).
_INLINE_IMAGE_MIME = frozenset(
    {"image/jpeg", "image/png", "image/gif", "image/webp"}
)


def _workspace_root(agent: Agent[Any]) -> Path | None:
    """The agent's local-workspace root, or None (mirrors ``workspace_cfg``)."""
    ws = agent.workspace
    cfg = getattr(ws, "workspace", ws)
    if isinstance(cfg, LocalWorkspace):
        return Path(cfg.root).expanduser().resolve()
    return None


def build_user_input(req: ChatRequest, agent: Agent[Any]) -> str | list[Message]:
    """The runner input for one turn: a plain string, or a multimodal message.

    Returns ``req.message`` unchanged when there are no usable attachments, so
    the common text-only path is untouched. Otherwise returns a single-element
    ``[user([...parts])]`` list carrying the text, any inlined images, and a
    note pointing at every attachment's workspace path.
    """
    attachments = req.attachments or []
    root = _workspace_root(agent)
    if not attachments or root is None:
        return req.message

    can_see = supports_vision(getattr(agent, "model", None))
    parts: list[ContentPart] = []
    if req.message.strip():
        parts.append(TextPart(req.message))

    rels: list[str] = []
    for att in attachments:
        resolved = resolve_path(root, att.path)
        if not resolved.inside or not resolved.abs.is_file():
            log.warning("dropping attachment outside workspace or missing: %r", att.path)
            continue
        rels.append(resolved.rel or att.path)
        if can_see and att.kind == "image" and att.mime in _INLINE_IMAGE_MIME:
            try:
                parts.append(ImagePart.from_path(resolved.abs, mime_type=att.mime))
            except (OSError, ValueError) as exc:  # pragma: no cover - defensive
                log.warning("could not inline image %s: %s", resolved.rel, exc)

    if not rels:
        return req.message  # every attachment was dropped → plain text turn

    # A path note so the agent can reach every file with its own tools (and any
    # image a non-vision model can't see). Separated from the user's own text
    # with a blank line so history replay doesn't run the two together.
    lead = "\n\n" if req.message.strip() else ""
    parts.append(TextPart(f"{lead}{_attachment_note(rels)}"))
    return [user(parts)]


def _attachment_note(rels: list[str]) -> str:
    """A one-line note telling the model where the attachments live on disk."""
    return (
        "[Attached in the workspace: "
        + ", ".join(rels)
        + ". Read with your file tools when relevant.]"
    )
