"""Single source of truth for image-type handling in the web UI.

Two deliberately-separate notions, each defined once here and imported wherever
it is needed (instead of ad-hoc literals scattered across modules):

* the **model** set — raster formats a vision model API accepts. Gates what is
  inlined as an :class:`~lovia.ImagePart`; delegates to the core
  :func:`lovia.parts.image_mime` (the same gate ``view_image`` and
  ``describe_image`` use), re-exposed here under the web-layer name
  (:func:`model_image_mime`).
* the **preview** predicate — formats a browser renders inline. Drives the
  upload ``kind`` (image vs file), the ``/api/workspace/raw`` inline preview,
  and the composer/Files-panel thumbnails (:func:`is_preview_image`). A superset
  of the model set.

SVG is in neither: it can carry scripts, so it is never inlined (download-only).
That is load-bearing, not incidental — the UI renders an SVG by pointing an
``<img>`` at the *attachment* form, which browsers ignore for a subresource
(where SVG scripts don't run) and honor for navigation (where they would). See
``workspaceImageUrl`` in ``static/js/util.js``.
The JS side mirrors the preview notion in ``static/js/util.js`` (``IMAGE_EXT``).
"""

from __future__ import annotations

from pathlib import Path

from ..parts import _IMAGE_MIME_BY_SUFFIX, image_mime

# Suffix → mime for the raster formats a vision model API accepts — the core
# table, aliased for the web modules that iterate it.
MODEL_IMAGE_MIME_BY_EXT = _IMAGE_MIME_BY_SUFFIX
MODEL_IMAGE_MIME = frozenset(MODEL_IMAGE_MIME_BY_EXT.values())

# Extension → mime for images a browser renders inline (thumbnails,
# /api/workspace/raw, the Files panel). Explicit rather than mimetypes.guess_type
# so the served Content-Type is stable across OSes — important now that /raw
# sends ``X-Content-Type-Options: nosniff``, which stops a browser from rendering
# an image whose Content-Type came back as application/octet-stream (guess_type
# doesn't know e.g. AVIF on every system). The JS side mirrors this EXACT key set
# in static/js/files.js (IMAGE_EXT) — keep the two in sync. SVG is excluded (it
# can carry scripts, never inlined); HEIC/TIFF are excluded (browsers don't
# render them inline).
PREVIEW_IMAGE_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "bmp": "image/bmp",
    "ico": "image/x-icon",
}
PREVIEW_IMAGE_EXT = frozenset(PREVIEW_IMAGE_MIME_BY_EXT)


def _ext(name: str | Path) -> str:
    return Path(str(name)).suffix.lower().lstrip(".")


def model_image_mime(name: str | Path) -> str | None:
    """The vision-model mime for a filename (by extension), or ``None`` when the
    model can't ingest it directly — the gate for inlining attachments. Same
    table as the core ``view_image``/``describe_image`` tools."""
    return image_mime(name)


def preview_image_mime(name: str | Path) -> str | None:
    """The stable Content-Type for a browser-previewable image, or ``None`` when
    the file isn't one. Prefer this over ``mimetypes.guess_type`` when serving a
    preview inline, so a nosniff'd response carries a correct image mime."""
    return PREVIEW_IMAGE_MIME_BY_EXT.get(_ext(name))


def is_preview_image(name: str | Path) -> bool:
    """True when a browser renders this image inline (thumbnails, ``/raw``, the
    Files panel), decided by file extension against :data:`PREVIEW_IMAGE_EXT`
    (which the UI's ``IMAGE_EXT`` mirrors). SVG is excluded — it can carry
    scripts, so it is never served inline."""
    return _ext(name) in PREVIEW_IMAGE_MIME_BY_EXT
