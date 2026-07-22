"""Vision-as-a-tool: let a text-only main model "see" images via a VLM.

When the app is configured with a dedicated vision model and the main model
itself isn't vision-capable, the CLI registers a ``see_image`` tool. The main
model calls it with a workspace image path (typically an ``uploads/…`` file the
user just attached) and a question; the tool runs a one-shot turn on the vision
model with the image inlined as an :class:`~lovia.ImagePart` and returns its
text answer.

Same "separate ad-hoc model for a sub-task" pattern as :mod:`lovia.web.titles`:
the main transcript only ever sees the returned text, never the image bytes, so
a text-only main model stays text-only. The reusable piece is
:func:`make_see_image_tool`; the CLI decides when to wire it in (see
``resolve_vision_tool`` in ``lovia.web.__main__``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from ..agent import Agent
from ..messages import user
from ..parts import ImagePart, TextPart
from ..providers import Provider
from ..runner import Runner
from ..tools import Tool, tool
from ..workspace.paths import resolve_path

log = logging.getLogger(__name__)

DEFAULT_QUESTION = "Describe this image in detail, including any text it contains."

VISION_INSTRUCTIONS = (
    "You are a vision assistant. Study the image and answer the question "
    "accurately and concisely, grounded only in what is visible. Transcribe "
    "any important text verbatim. If the image does not answer the question, "
    "say so plainly."
)

# Suffix → mime for the formats ImagePart can inline. Kept explicit so an
# unsupported file fails with a clear message the model can act on, rather than
# a raw ValueError out of ImagePart.from_path.
_IMAGE_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


def make_see_image_tool(
    vision_model: str | Provider, *, workspace_root: str | Path
) -> Tool:
    """Build a ``see_image`` tool backed by ``vision_model``.

    ``vision_model`` is anything :class:`Agent` accepts — a ``"vendor:model"``
    string or a :class:`~lovia.providers.Provider`. ``workspace_root`` bounds
    which files the tool may read: a path resolving outside the root is
    refused, so the model can't turn this into an arbitrary file reader.
    """
    root = Path(workspace_root).expanduser().resolve()
    viewer: Agent[Any] = Agent(
        name="vision", instructions=VISION_INSTRUCTIONS, model=vision_model
    )

    @tool(
        name="see_image",
        description=(
            "Look at an image file in the workspace and answer a question about "
            "it. Images the user attaches land under 'uploads/'. Use this "
            "whenever the user refers to an image you cannot otherwise see."
        ),
    )
    async def see_image(
        path: Annotated[
            str, "Workspace-relative path to the image, e.g. 'uploads/photo.png'."
        ],
        question: Annotated[
            str, "What to ask about the image; defaults to a full description."
        ] = DEFAULT_QUESTION,
    ) -> str:
        resolved = resolve_path(root, path)
        if not resolved.inside:
            return f"Error: {path!r} is outside the workspace; refusing to read it."
        abs_path = resolved.abs
        if not abs_path.is_file():
            return f"Error: no such image in the workspace: {resolved.display()}"
        mime = _IMAGE_MIME.get(abs_path.suffix.lower().lstrip("."))
        if mime is None:
            return (
                f"Error: {resolved.display()} is not a supported image type "
                "(supported: jpg, png, gif, webp)."
            )
        try:
            image = ImagePart.from_path(abs_path, mime_type=mime)
            result = await Runner.run(viewer, [user([TextPart(question), image])])
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("see_image failed for %s: %s", resolved.display(), exc)
            return f"Error: could not analyze the image ({exc})."
        return result.output if isinstance(result.output, str) else str(result.output)

    return see_image
