"""What a provider's refusal says about the images in its request.

A provider that refuses an image answers 400 (or 413 / 422) and the run used
to end there (#532). The runner instead withholds images and sends the call
again (``lovia/runtime/images.py``); this module only reads the refusal.
Two answers matter, because they are remembered differently:

* ``"unsupported"`` — the model or endpoint takes no images at all. Only an
  explicit statement counts: the runner remembers it for the whole chat.
* ``"image"`` — something about these images (too many pixels, too many
  images, bytes it could not decode, or a refusal that names images without
  saying why). Recovered from, never taken as a fact about the model: the
  first image a vision model ever sees may be an oversized screenshot.

Whether the request actually carried an image is the runner's to check —
the same words in an answer to an image-free request mean something else.
"""

from __future__ import annotations

import re
from typing import Literal

from ..exceptions import ProviderError

ImageRejection = Literal["unsupported", "image"]

# The statuses a refused request comes back with. 408 is a timeout (retried
# as a network error), 401/403/404/429 are never about the payload.
_STATUSES = frozenset({400, 413, 422})

_MENTIONS_IMAGE = re.compile(r"image|multi-?modal|vision", re.I)

# "The model takes no images", as endpoints say it. Narrow on purpose: this
# answer is remembered for the chat, so a vague one must not land here — nor
# one about a format ("Image format is not supported", "does not support
# image/gif"), which is about this image.
_UNSUPPORTED = re.compile(
    r"does(?: not|n'?t) support (?:the )?(?:images?|vision|multi-?modal)"
    r"(?: (?:input|inputs|content|messages?))?(?![\w/-])"
    r"|(?:image (?:input|content)s?|images|vision|multi-?modal (?:input|content)s?)"
    r" (?:is|are) not supported"
    r"|not a multi-?modal model"
    r"|unknown variant [`'\"]?image_url"
    r"|image_url[\w\s.,'`\"]{0,40}only supported by certain models",
    re.I,
)

# What makes a refusal a context overflow whatever it says of images: "the
# combined text and image tokens exceed the context window" is compacted.
_TOKENS = re.compile(r"token|context", re.I)

# An image refused for its size or number. Read before the context-overflow
# needles: "request too large" with these words is about an image, and
# reactive compaction cannot shrink it.
_IMAGE_LIMIT = re.compile(
    r"image[\w\s/,.'`\"-]{0,60}(?:pixel|dimension|exceed|too large|too big|size|maximum)"
    r"|(?:pixel|dimension|exceed|too large|too big|size|maximum)[\w\s/,.'`\"-]{0,60}image"
    r"|too many images",
    re.I,
)


def names_image_limit(text: str) -> bool:
    """Whether an error body blames an image's size or the number of images —
    and not the prompt's tokens or the context window."""
    return bool(_IMAGE_LIMIT.search(text)) and not _TOKENS.search(text)


def image_rejection(exc: BaseException) -> ImageRejection | None:
    """What ``exc`` says about the images its request carried, or ``None``
    when it is not a refusal about images."""
    if not isinstance(exc, ProviderError) or exc.status_code not in _STATUSES:
        return None
    body = getattr(exc, "body", None)  # set by the built-in adapters
    text = body if isinstance(body, str) and body else str(exc)
    if not _MENTIONS_IMAGE.search(text):
        return None
    return "unsupported" if _UNSUPPORTED.search(text) else "image"


__all__ = ["ImageRejection", "image_rejection", "names_image_limit"]
