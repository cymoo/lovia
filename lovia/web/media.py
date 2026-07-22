"""Single source of truth for image-type handling in the web UI.

Two deliberately-separate notions, each defined once here and imported wherever
it is needed (instead of ad-hoc literals scattered across modules):

* the **model** set — raster formats a vision model API accepts. Gates what is
  inlined as an :class:`~lovia.ImagePart` and what ``see_image`` will read
  (:func:`model_image_mime`).
* the **preview** predicate — formats a browser renders inline. Drives the
  upload ``kind`` (image vs file), the ``/api/workspace/raw`` inline preview,
  and the composer/Files-panel thumbnails (:func:`is_preview_image`). A superset
  of the model set.

SVG is in neither: it can carry scripts, so it is never inlined (download-only).
The JS side mirrors the preview notion in ``static/js/files.js`` (``IMAGE_EXT``).
"""

from __future__ import annotations

from pathlib import Path

# Suffix → mime for the raster formats a vision model API accepts. Explicit so
# an unsupported file fails with a clear message rather than a raw ValueError
# out of ImagePart.from_path.
MODEL_IMAGE_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}
MODEL_IMAGE_MIME = frozenset(MODEL_IMAGE_MIME_BY_EXT.values())


def model_image_mime(name: str | Path) -> str | None:
    """The vision-model mime for a filename (by extension), or ``None`` when the
    model can't ingest it directly — the gate for inlining and ``see_image``."""
    return MODEL_IMAGE_MIME_BY_EXT.get(Path(str(name)).suffix.lower().lstrip("."))


def is_preview_image(mime: str | None) -> bool:
    """True when a browser renders this image inline (thumbnails, ``/raw``).

    Any ``image/*`` except SVG, which can carry scripts and is never inlined.
    """
    return mime is not None and mime.startswith("image/") and mime != "image/svg+xml"
