"""Shared provider wire-format conversion helpers."""

from __future__ import annotations

from typing import Any

from ..content import ImageBlock, TextBlock


def _image_url(block: ImageBlock) -> str:
    if block.url is not None:
        return block.url
    return f"data:{block.mime_type};base64,{block.data}"


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
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content block: {block!r}")
    return parts


def content_to_responses_input(content: str | list[Any]) -> list[dict[str, Any]]:
    """Convert lovia content blocks to OpenAI Responses input blocks."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            out.append({"type": "input_text", "text": block.text})
        elif isinstance(block, ImageBlock):
            entry: dict[str, Any] = {
                "type": "input_image",
                "image_url": _image_url(block),
            }
            if block.detail is not None:
                entry["detail"] = block.detail
            out.append(entry)
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content block: {block!r}")
    return out


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
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


# TODO: likely useless
def openai_chat_tool_to_responses(tool: dict[str, Any]) -> dict[str, Any]:
    """Flatten an OpenAI Chat function tool schema to Responses format."""
    if tool.get("type") != "function":
        return tool
    fn = tool.get("function", {})
    out: dict[str, Any] = {"type": "function", "name": fn["name"]}
    if "description" in fn:
        out["description"] = fn["description"]
    if "parameters" in fn:
        out["parameters"] = fn["parameters"]
    return out
