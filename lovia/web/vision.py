"""Vision-as-a-tool: let a text-only main model "see" images via a VLM.

When the app is configured with a dedicated vision model, the builder registers
a ``describe_image`` tool, and a model call offers it while the main model
can't see images (``describes_images``: its provider says so, or it refused
them in this chat). The main model calls it with an image's path (one the
user attached, as the message's note names it, or one the run produced) and
a question; the tool runs a one-shot turn on the vision model with the image
inlined as an :class:`~lovia.ImagePart` and returns its text answer.

The name states the contract: the model gets a *description*, never the
pixels — the counterpart of the core ``view_image`` tool, which returns the
image itself and is what a model that sees gets instead (a call offers one
or the other).

Same "separate ad-hoc model for a sub-task" pattern as :mod:`lovia.web.titles`.
Paths go through the same read pipeline as ``read_file``/``view_image``: the
workspace session enforces the path ACL (``read_outside`` honored), the
approval predicate resolves the ask side, and the shared 3.75 MB cap applies.
"""

from __future__ import annotations

import base64
import logging
from typing import Annotated, Any

from .. import events
from ..agent import Agent
from ..exceptions import ToolError
from ..messages import user
from ..parts import ImagePart, TextPart, image_mime, sniff_image_mime
from ..providers import Provider, supports_vision
from ..run_context import RunContext
from ..runner import Runner
from ..tools import Tool, tool
from ..workspace.errors import FileTooLargeError
from ..workspace.tools import (
    VIEW_IMAGE_MAX_BYTES,
    path_permission,
    require_workspace,
)

log = logging.getLogger(__name__)

_REFUSED = (
    "The vision model refused the image {path} (too large, an unsupported "
    "format, or a model that takes no images); it could not be described."
)

# The tool's name: stored in transcripts, and how a rebuilt agent finds it.
DESCRIBE_IMAGE = "describe_image"
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
        name=DESCRIBE_IMAGE,
        description=(
            "Have an image file described to you (you will get text, not the "
            "image). The paths of images the user attaches are listed in their "
            "message's [Attached: …] note. Use this whenever the user refers "
            "to an image you cannot otherwise see.\n"
            "- path is workspace-relative or absolute; outside paths may need "
            "approval or be denied by policy.\n"
            "- Supported: jpg, jpeg, png, gif, webp; over 3.75 MB is refused."
        ),
        permission=path_permission("read"),
        describes_images=True,
    )
    async def describe_image(
        ctx: RunContext[Any],
        path: Annotated[
            str,
            "Path to the image: workspace-relative, or an attachment's absolute path.",
        ],
        question: Annotated[
            str, "What to ask about the image; defaults to a full description."
        ] = DEFAULT_QUESTION,
    ) -> str:
        # A vision model whose provider says it takes no images would get the
        # image's text marker (the runner withholds it without asking) and
        # "describe" that.
        # (A "vendor:model" string resolves to a provider that does.)
        if not isinstance(vision_model, str) and not supports_vision(vision_model):
            raise ToolError(_REFUSED.format(path=path))
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
        # Sent as the type its bytes are: a provider refuses a mismatch.
        sniffed = sniff_image_mime(file.data)
        if sniffed is None:
            raise ToolError(
                f"Not a supported image: {path!r} is not PNG, JPEG, GIF or WebP inside"
            )
        image = ImagePart(
            data=base64.b64encode(file.data).decode("ascii"), mime_type=sniffed
        )
        # A vision model that refuses the image would otherwise "describe"
        # its text marker (the runner withholds a refused image and goes on):
        # stop at the first sign of that and say so instead.
        handle = Runner.stream(viewer, [user([TextPart(question), image])])
        refused = False
        try:
            async for ev in handle:
                if isinstance(ev, events.ImagesWithheld) and not refused:
                    refused = True
                    handle.cancel("the vision model refused the image")
            result = await handle.result()
        except Exception as exc:
            if refused:
                raise ToolError(_REFUSED.format(path=file.path)) from exc
            log.warning("describe_image failed for %s: %s", file.path, exc)
            raise ToolError(f"Could not analyze the image ({exc}).") from exc
        if refused:  # pragma: no cover - a cancelled run raises above
            raise ToolError(_REFUSED.format(path=file.path))
        return result.output if isinstance(result.output, str) else str(result.output)

    return describe_image
