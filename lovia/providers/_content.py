"""Shared provider wire-format conversion helpers."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from ..content import FileBlock, ImageBlock, TextBlock
from ..exceptions import UserError


def _image_url(block: ImageBlock) -> str:
    if block.url is not None:
        return block.url
    return f"data:{block.mime_type};base64,{block.data}"


def _openai_file(block: FileBlock) -> dict[str, Any]:
    if block.url is not None:
        raise UserError(
            "OpenAI Chat provider does not support FileBlock URL inputs",
            hint="Use FileBlock.from_path/from_bytes for inline files, use ImageBlock for image URLs, or choose Anthropic for provider-native PDF URLs.",
        )
    if block.data is None:  # pragma: no cover - FileBlock validates this.
        raise TypeError(f"Unsupported file block: {block!r}")
    file: dict[str, Any] = {"file_data": block.data}
    if block.filename is not None:
        file["filename"] = block.filename
    return file


def _anthropic_file_source(block: FileBlock) -> dict[str, Any]:
    if block.url is not None:
        if block.mime_type not in (None, "application/pdf"):
            raise UserError(
                f"Anthropic document URLs require application/pdf, got {block.mime_type!r}",
                hint="Use a PDF URL, send text as TextBlock, or embed local PDF/text data with FileBlock.from_path/from_bytes.",
            )
        return {"type": "url", "url": block.url}

    if block.data is None:  # pragma: no cover - FileBlock validates this.
        raise TypeError(f"Unsupported file block: {block!r}")

    if block.mime_type == "application/pdf":
        return {
            "type": "base64",
            "media_type": "application/pdf",
            "data": block.data,
        }

    if block.mime_type == "text/plain":
        try:
            text = base64.b64decode(block.data, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise UserError(
                "Anthropic text FileBlock data must be valid UTF-8 base64",
                hint="Use FileBlock.from_bytes(..., mime_type='text/plain') for local text files, or pass text directly as TextBlock.",
            ) from exc
        return {"type": "text", "media_type": "text/plain", "data": text}

    raise UserError(
        f"Anthropic document inputs support application/pdf or text/plain, got {block.mime_type!r}",
        hint="Convert unsupported documents to PDF, pass extracted text as TextBlock, or use Anthropic's Files API outside lovia.",
    )


def content_to_openai_chat(content: str | list[Any]) -> str | list[dict[str, Any]]:
    """Convert lovia content blocks to OpenAI Chat content."""
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            image_url: dict[str, Any] = {"url": _image_url(block)}
            if block.detail is not None:
                image_url["detail"] = block.detail
            parts.append({"type": "image_url", "image_url": image_url})
        elif isinstance(block, FileBlock):
            parts.append({"type": "file", "file": _openai_file(block)})
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content block: {block!r}")
    return parts


def content_to_anthropic_blocks(content: str | list[Any]) -> list[dict[str, Any]]:
    """Convert lovia content blocks to Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            out.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            if block.url is not None:
                source: dict[str, Any] = {"type": "url", "url": block.url}
            else:
                source = {
                    "type": "base64",
                    "media_type": block.mime_type,
                    "data": block.data,
                }
            out.append({"type": "image", "source": source})
        elif isinstance(block, FileBlock):
            document = {"type": "document", "source": _anthropic_file_source(block)}
            if block.filename is not None:
                document["title"] = block.filename
            out.append(document)
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content block: {block!r}")
    return out


def text_only(content: str | list[Any] | None) -> str:
    """Flatten content blocks to plain text for fields that do not accept blocks."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def openai_tool_to_anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI Chat function tool schema to Anthropic's shape."""
    fn = tool.get("function") or {}
    out = {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }
    if "strict" in fn:
        out["strict"] = fn["strict"]
    return out
