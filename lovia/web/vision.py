"""Vision-as-a-tool: let a text-only main model "see" images via a VLM.

When the app is configured with a dedicated vision model and the main model
itself isn't vision-capable, the builder registers a ``describe_image`` tool.
The main model calls it with a workspace image path (an ``uploads/…`` file the
user attached, or one the run produced) and a question; the tool runs a
one-shot turn on the vision model with the image inlined as an
:class:`~lovia.ImagePart` and returns its text answer.

The name states the contract: the model gets a *description*, never the
pixels — the counterpart of the core ``view_image`` tool, which returns the
image itself and is what a vision-capable main model gets instead (the two
are never registered together; see ``resolve_vision_tool`` in
``lovia.web.builder``).

Same "separate ad-hoc model for a sub-task" pattern as :mod:`lovia.web.titles`.
Paths go through the same read pipeline as ``read_file``/``view_image``: the
workspace session enforces the path ACL (``read_outside`` honored), the
approval predicate resolves the ask side, and the shared 5 MB cap applies.
"""

from __future__ import annotations

import base64
import logging
from typing import Annotated, Any

from ..agent import Agent
from ..exceptions import ToolError
from ..messages import user
from ..parts import ImagePart, TextPart, image_mime
from ..providers import Provider
from ..run_context import RunContext
from ..runner import Runner
from ..tools import Tool, tool
from ..workspace.errors import FileTooLargeError
from ..workspace.tools import (
    VIEW_IMAGE_MAX_BYTES,
    path_needs_approval,
    require_workspace,
)

log = logging.getLogger(__name__)

DEFAULT_QUESTION = "Describe this image in detail, including any text it contains."

VISION_INSTRUCTIONS = (
    "You are a vision assistant. Study the image and answer the question "
    "accurately and concisely, grounded only in what is visible. Transcribe "
    "any important text verbatim. If the image does not answer the question, "
    "say so plainly."
)


def make_describe_image_tool(vision_model: str | Provider) -> Tool:
    """Build a ``describe_image`` tool backed by ``vision_model``.

    ``vision_model`` is anything :class:`Agent` accepts — a ``"vendor:model"``
    string or a :class:`~lovia.providers.Provider`. The image is read through
    the run's workspace session, so the workspace policy bounds what the tool
    may look at exactly like ``read_file``.
    """
    viewer: Agent[Any] = Agent(
        name="vision", instructions=VISION_INSTRUCTIONS, model=vision_model
    )

    @tool(
        name="describe_image",
        description=(
            "Have an image file described to you (you will get text, not the "
            "image). Images the user attaches land under 'uploads/'. Use this "
            "whenever the user refers to an image you cannot otherwise see.\n"
            "- path is workspace-relative or absolute; outside paths may need "
            "approval or be denied by policy.\n"
            "- Supported: jpg, jpeg, png, gif, webp; over 5 MB is refused."
        ),
        needs_approval=path_needs_approval("read"),
    )
    async def describe_image(
        ctx: RunContext[Any],
        path: Annotated[
            str, "Workspace-relative path to the image, e.g. 'uploads/photo.png'."
        ],
        question: Annotated[
            str, "What to ask about the image; defaults to a full description."
        ] = DEFAULT_QUESTION,
    ) -> str:
        session = require_workspace(ctx)
        mime = image_mime(path)
        if mime is None:
            raise ToolError(
                f"Not a supported image type: {path!r}",
                hint="describe_image reads jpg, jpeg, png, gif, or webp files.",
            )
        try:
            file = await session.read_bytes(path, max_bytes=VIEW_IMAGE_MAX_BYTES)
        except FileTooLargeError as exc:
            raise ToolError(
                str(exc),
                hint=(
                    "Downscale it first via the shell, e.g. `sips -Z 1568 "
                    f"{path}` (macOS) or `magick {path} -resize 1568x1568 "
                    f"{path}`, then try again."
                ),
            ) from exc
        image = ImagePart(
            data=base64.b64encode(file.data).decode("ascii"), mime_type=mime
        )
        try:
            result = await Runner.run(viewer, [user([TextPart(question), image])])
        except Exception as exc:
            log.warning("describe_image failed for %s: %s", file.path, exc)
            raise ToolError(f"Could not analyze the image ({exc}).") from exc
        return result.output if isinstance(result.output, str) else str(result.output)

    return describe_image
