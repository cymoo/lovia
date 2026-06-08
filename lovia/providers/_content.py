"""Shared provider wire-format conversion helpers."""

from __future__ import annotations

import base64
import binascii
from typing import cast

from .._types import JsonObject
from ..content import ContentPart, FilePart, ImagePart, TextPart
from ..exceptions import UserError


def _image_url(part: ImagePart) -> str:
    if part.url is not None:
        return part.url
    return f"data:{part.mime_type};base64,{part.data}"


def _openai_file(part: FilePart) -> JsonObject:
    if part.url is not None:
        raise UserError(
            "OpenAI Chat provider does not support FilePart URL inputs",
            hint="Use FilePart.from_path/from_bytes for inline files, use ImagePart for image URLs, or choose Anthropic for provider-native PDF URLs.",
        )
    if part.data is None:  # pragma: no cover - FilePart validates this.
        raise TypeError(f"Unsupported file part: {part!r}")
    file: JsonObject = {"file_data": part.data}
    if part.filename is not None:
        file["filename"] = part.filename
    return file


def _anthropic_file_source(part: FilePart) -> JsonObject:
    if part.url is not None:
        if part.mime_type not in (None, "application/pdf"):
            raise UserError(
                f"Anthropic document URLs require application/pdf, got {part.mime_type!r}",
                hint="Use a PDF URL, send text as TextPart, or embed local PDF/text data with FilePart.from_path/from_bytes.",
            )
        return {"type": "url", "url": part.url}

    if part.data is None:  # pragma: no cover - FilePart validates this.
        raise TypeError(f"Unsupported file part: {part!r}")

    if part.mime_type == "application/pdf":
        return {
            "type": "base64",
            "media_type": "application/pdf",
            "data": part.data,
        }

    if part.mime_type == "text/plain":
        try:
            text = base64.b64decode(part.data, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise UserError(
                "Anthropic text FilePart data must be valid UTF-8 base64",
                hint="Use FilePart.from_bytes(..., mime_type='text/plain') for local text files, or pass text directly as TextPart.",
            ) from exc
        return {"type": "text", "media_type": "text/plain", "data": text}

    raise UserError(
        f"Anthropic document inputs support application/pdf or text/plain, got {part.mime_type!r}",
        hint="Convert unsupported documents to PDF, pass extracted text as TextPart, or use Anthropic's Files API outside lovia.",
    )


def content_to_openai_chat(content: str | list[ContentPart]) -> str | list[JsonObject]:
    """Convert lovia content parts to OpenAI Chat content."""
    if isinstance(content, str):
        return content
    parts: list[JsonObject] = []
    for part in content:
        if isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            image_url: JsonObject = {"url": _image_url(part)}
            if part.detail is not None:
                image_url["detail"] = part.detail
            parts.append({"type": "image_url", "image_url": image_url})
        elif isinstance(part, FilePart):
            parts.append({"type": "file", "file": _openai_file(part)})
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content part: {part!r}")
    return parts


def content_to_anthropic_blocks(content: str | list[ContentPart]) -> list[JsonObject]:
    """Convert lovia content parts to Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out: list[JsonObject] = []
    for part in content:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            if part.url is not None:
                source: JsonObject = {"type": "url", "url": part.url}
            else:
                source = {
                    "type": "base64",
                    "media_type": part.mime_type,
                    "data": part.data,
                }
            out.append({"type": "image", "source": source})
        elif isinstance(part, FilePart):
            document: JsonObject = {
                "type": "document",
                "source": _anthropic_file_source(part),
            }
            if part.filename is not None:
                document["title"] = part.filename
            out.append(document)
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content part: {part!r}")
    return out


def text_only(content: str | list[ContentPart] | None) -> str:
    """Flatten content parts to plain text for fields that do not accept parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def openai_tool_to_anthropic(tool: JsonObject) -> JsonObject:
    """Convert an OpenAI Chat function tool schema to Anthropic's shape."""
    fn = cast(JsonObject, tool.get("function") or {})
    out: JsonObject = {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }
    if "strict" in fn:
        out["strict"] = fn["strict"]
    return out
