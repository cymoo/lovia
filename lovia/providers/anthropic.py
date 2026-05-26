"""Anthropic Messages API provider.

Translates lovia's OpenAI-shaped internal message format into the Anthropic
Messages API and back. We talk HTTP directly so the ``anthropic`` SDK is not
required.

Conversions worth noting:

* The Anthropic API takes a separate ``system`` parameter (a string), not a
  message with role ``system``. We extract the first system message.
* ``tool`` role messages become ``user`` messages whose content is a list
  containing a ``tool_result`` block keyed by ``tool_use_id``.
* Assistant tool calls become ``tool_use`` content blocks; we generate the
  ``id`` mapping on the fly.
* Streaming uses Anthropic's SSE event types (``content_block_delta``,
  ``message_delta``, ...) which we translate into :class:`StreamChunk`.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

from ..exceptions import ProviderError
from ..messages import AssistantMessage, ChatMessage, ToolCall, Usage
from .base import ModelSettings, StreamChunk, ToolCallDelta


_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_VERSION = "2023-06-01"


class AnthropicProvider:
    """Anthropic Messages API adapter."""

    name = "anthropic"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
        anthropic_version: str = _DEFAULT_VERSION,
        default_max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._version = anthropic_version
        self._default_max_tokens = default_max_tokens

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": self._version,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    def _build_payload(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        settings: ModelSettings | None,
        stream: bool,
    ) -> dict[str, Any]:
        system_text, anthropic_messages = _to_anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": (settings.max_tokens if settings and settings.max_tokens else self._default_max_tokens),
        }
        if system_text:
            payload["system"] = system_text
        if tools:
            payload["tools"] = [_openai_tool_to_anthropic(t) for t in tools]
        if stream:
            payload["stream"] = True
        if settings is not None:
            if settings.temperature is not None:
                payload["temperature"] = settings.temperature
            if settings.top_p is not None:
                payload["top_p"] = settings.top_p
            if settings.stop is not None:
                payload["stop_sequences"] = settings.stop
            payload.update(settings.extra)
        return payload

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AssistantMessage:
        # Anthropic ignores response_format for now; structured output falls
        # back to the final_output tool, handled one layer up.
        _ = response_format
        payload = self._build_payload(messages, tools, settings, stream=False)
        try:
            response = await self._client.post(
                f"{self.base_url}/messages",
                headers=self._headers(),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"Anthropic returned HTTP {response.status_code}: {response.text}"
            )
        return _parse_anthropic_response(response.json())

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[StreamChunk]:
        _ = response_format
        payload = self._build_payload(messages, tools, settings, stream=True)

        text_buf: list[str] = []
        # Tool call assembly: index -> {id, name, arguments}
        tool_calls: dict[int, dict[str, str]] = {}
        block_kinds: dict[int, str] = {}
        usage = Usage()
        stop_reason: str | None = None

        async with self._client.stream(
            "POST",
            f"{self.base_url}/messages",
            headers=self._headers(),
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise ProviderError(
                    f"Anthropic stream returned HTTP {response.status_code}: {body.decode(errors='replace')}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[len("data:") :].strip())
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")

                if etype == "content_block_start":
                    idx = event.get("index", 0)
                    block = event.get("content_block") or {}
                    block_kinds[idx] = block.get("type", "")
                    if block.get("type") == "tool_use":
                        tool_calls[idx] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "arguments": "",
                        }
                        yield StreamChunk(
                            tool_call_delta=ToolCallDelta(
                                index=idx, id=block.get("id"), name=block.get("name")
                            )
                        )
                elif etype == "content_block_delta":
                    idx = event.get("index", 0)
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        chunk_text = delta.get("text", "")
                        text_buf.append(chunk_text)
                        yield StreamChunk(text_delta=chunk_text)
                    elif delta.get("type") == "input_json_delta":
                        partial = delta.get("partial_json", "")
                        if idx in tool_calls:
                            tool_calls[idx]["arguments"] += partial
                        yield StreamChunk(
                            tool_call_delta=ToolCallDelta(index=idx, arguments_delta=partial)
                        )
                elif etype == "message_delta":
                    stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                    if (u := event.get("usage")):
                        usage.output_tokens = u.get("output_tokens", usage.output_tokens)
                elif etype == "message_start":
                    if (u := (event.get("message") or {}).get("usage")):
                        usage.input_tokens = u.get("input_tokens", 0)
                        usage.output_tokens = u.get("output_tokens", 0)
                elif etype == "message_stop":
                    break

        final = AssistantMessage(
            content="".join(text_buf) or None,
            tool_calls=[
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"] or "{}")
                for _, tc in sorted(tool_calls.items())
            ],
            usage=usage,
            finish_reason=_normalize_stop_reason(stop_reason),
        )
        yield StreamChunk(done=final)


def _normalize_stop_reason(reason: str | None) -> str | None:
    """Map Anthropic stop reasons to OpenAI-style ``finish_reason`` strings."""
    if reason is None:
        return None
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
    }.get(reason, reason)


def _to_anthropic_messages(
    messages: list[ChatMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Translate internal messages into Anthropic's API shape."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role == "system":
            if msg.content:
                system_parts.append(msg.content)
            continue

        if msg.role == "tool":
            # Tool results are user messages wrapping a tool_result content block.
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": msg.content or "",
                        }
                    ],
                }
            )
            continue

        if msg.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                # Anthropic expects parsed JSON in ``input``; degrade gracefully
                # if the model emitted invalid JSON.
                try:
                    parsed = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    parsed = {"_raw": tc.arguments}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": parsed,
                    }
                )
            out.append({"role": "assistant", "content": blocks or ""})
            continue

        # user
        out.append({"role": "user", "content": msg.content or ""})

    system_text = "\n\n".join(system_parts) if system_parts else None
    return system_text, out


def _openai_tool_to_anthropic(tool: dict[str, Any]) -> dict[str, Any]:
    fn = tool.get("function") or {}
    return {
        "name": fn.get("name", ""),
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def _parse_anthropic_response(data: dict[str, Any]) -> AssistantMessage:
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in data.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            content_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=json.dumps(block.get("input") or {}),
                )
            )
    usage_data = data.get("usage") or {}
    usage = Usage(
        input_tokens=usage_data.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
    )
    return AssistantMessage(
        content="".join(content_parts) or None,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=_normalize_stop_reason(data.get("stop_reason")),
    )
