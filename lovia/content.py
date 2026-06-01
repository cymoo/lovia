"""Structured content blocks for messages.

A :class:`ChatMessage`'s ``content`` may be either:

* a plain ``str`` — the common case, equivalent to a single :class:`TextBlock`;
* a ``list[ContentBlock]`` — a heterogeneous sequence of typed parts when the
  message carries images or files alongside (or instead of) text.

Providers translate these blocks into their wire format (OpenAI ``image_url`` /
``file``, Anthropic ``image`` / ``document`` source, …). Provider adapters never
inspect raw dicts.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Union


@dataclass
class TextBlock:
    """A run of plain UTF-8 text."""

    text: str
    type: Literal["text"] = "text"


@dataclass
class ImageBlock:
    """An image part.

    Exactly one of ``url`` or ``data`` (base64-encoded bytes) must be set.
    ``mime_type`` is required when ``data`` is set; it is inferred or left
    blank for URLs.
    """

    url: str | None = None
    data: str | None = None
    mime_type: str | None = None
    detail: Literal["auto", "low", "high"] | None = None
    type: Literal["image"] = "image"

    def __post_init__(self) -> None:
        if (self.url is None) == (self.data is None):
            raise ValueError("ImageBlock requires exactly one of url or data")
        if self.data is not None and self.mime_type is None:
            raise ValueError("ImageBlock with data also needs mime_type")

    @classmethod
    def from_path(
        cls, path: str | Path, *, mime_type: str | None = None
    ) -> "ImageBlock":
        """Load an image from disk and embed it as base64."""
        p = Path(path)
        if mime_type is None:
            suffix = p.suffix.lower().lstrip(".")
            mime_type = {
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "png": "image/png",
                "gif": "image/gif",
                "webp": "image/webp",
            }.get(suffix)
            if mime_type is None:
                raise ValueError(f"Cannot infer mime_type for {p.suffix!r}")
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        return cls(data=data, mime_type=mime_type)


@dataclass
class FileBlock:
    """A document or file part.

    Exactly one of ``url`` or ``data`` (base64-encoded bytes) must be set.
    ``mime_type`` is required when ``data`` is set. URL blocks are provider-native
    references; lovia does not download them.
    """

    url: str | None = None
    data: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    type: Literal["file"] = "file"

    def __post_init__(self) -> None:
        if (self.url is None) == (self.data is None):
            raise ValueError("FileBlock requires exactly one of url or data")
        if self.data is not None and self.mime_type is None:
            raise ValueError("FileBlock with data also needs mime_type")
        if self.data is not None:
            try:
                base64.b64decode(self.data, validate=True)
            except binascii.Error as exc:
                raise ValueError("FileBlock data must be valid base64") from exc

    @classmethod
    def from_path(
        cls,
        path: str | Path,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> "FileBlock":
        """Load a file from disk and embed it as base64."""
        p = Path(path)
        if mime_type is None:
            mime_type = mimetypes.guess_type(p.name)[0]
            if mime_type is None:
                raise ValueError(f"Cannot infer mime_type for {p.suffix!r}")
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        return cls(data=data, mime_type=mime_type, filename=filename or p.name)

    @classmethod
    def from_bytes(
        cls,
        data: bytes,
        *,
        mime_type: str,
        filename: str | None = None,
    ) -> "FileBlock":
        """Embed in-memory bytes as base64."""
        encoded = base64.b64encode(data).decode("ascii")
        return cls(data=encoded, mime_type=mime_type, filename=filename)

    @classmethod
    def from_base64(
        cls,
        data: str,
        *,
        mime_type: str,
        filename: str | None = None,
    ) -> "FileBlock":
        """Wrap already-base64-encoded file data."""
        return cls(data=data, mime_type=mime_type, filename=filename)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> "FileBlock":
        """Reference a provider-accessible URL without downloading it."""
        return cls(url=url, mime_type=mime_type, filename=filename)


ContentBlock = Union[TextBlock, ImageBlock, FileBlock]
"""Discriminated union of all content block types."""


def normalize_content(
    content: "str | ContentBlock | list[ContentBlock] | None",
) -> "str | list[ContentBlock] | None":
    """Coerce loose input into the canonical ``str | list[ContentBlock]`` form."""
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, (TextBlock, ImageBlock, FileBlock)):
        return [content]
    return list(content)


def text_of(content: "str | list[ContentBlock] | None") -> str:
    """Best-effort flattening of ``content`` to a plain string.

    Useful for logging, hooks, and provider adapters that need a textual
    summary when the wire format only supports strings.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ImageBlock):
            parts.append("[image]")
        elif isinstance(block, FileBlock):
            parts.append(f"[file:{block.filename}]" if block.filename else "[file]")
    return "".join(parts)
