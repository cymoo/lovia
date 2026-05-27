"""OpenAI Chat Completions provider.

This adapter speaks the OpenAI Chat Completions HTTP API directly via
``httpx``. It does not depend on the ``openai`` SDK, which keeps the install
footprint small and lets us point at any compatible endpoint (DeepSeek, Qwen,
Kimi, Ollama, vLLM, LM Studio, ...) by setting ``base_url``.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

from ..content import ImageBlock, TextBlock
from ..exceptions import ProviderError
from ..messages import AssistantMessage, ChatMessage, ToolCall, Usage
from .base import ModelSettings, StreamChunk, ToolCallDelta


_DEFAULT_BASE_URL = "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Wire-format serialization (OpenAI Chat Completions schema)
#
# Kept here — not on ``ChatMessage`` itself — so the core message type stays
# vendor-neutral. Other providers translate their own way.


def _content_to_openai(
    content: "str | list[Any]",
) -> "str | list[dict[str, Any]]":
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            if block.url is not None:
                image_url: dict[str, Any] = {"url": block.url}
            else:
                image_url = {"url": f"data:{block.mime_type};base64,{block.data}"}
            if block.detail is not None:
                image_url["detail"] = block.detail
            parts.append({"type": "image_url", "image_url": image_url})
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content block: {block!r}")
    return parts


def _tool_call_to_openai(tc: ToolCall) -> dict[str, Any]:
    return {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.name, "arguments": tc.arguments},
    }


def message_to_openai(msg: ChatMessage) -> dict[str, Any]:
    """Serialize a :class:`ChatMessage` to the OpenAI Chat Completions wire format."""
    out: dict[str, Any] = {"role": msg.role}
    if msg.content is not None:
        out["content"] = _content_to_openai(msg.content)
    if msg.reasoning_content is not None and msg.role == "assistant":
        out["reasoning_content"] = msg.reasoning_content
    if msg.tool_calls:
        out["tool_calls"] = [_tool_call_to_openai(tc) for tc in msg.tool_calls]
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    if msg.name is not None and msg.role in ("user", "assistant"):
        out["name"] = msg.name
    return out


class OpenAIChatProvider:
    """OpenAI Chat Completions API adapter.

    Args:
        model: The model identifier sent to the API (e.g. ``"gpt-4o-mini"``).
        api_key: API key. Defaults to ``$OPENAI_API_KEY``.
        base_url: Override to target an OpenAI-compatible endpoint.
        client: Optional pre-built :class:`httpx.AsyncClient`. If omitted we
            create one per provider instance and reuse it.
        timeout: Request timeout in seconds.
        default_headers: Extra headers merged into every request (useful for
            providers that require custom auth headers).
    """

    name = "openai-chat"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
        default_headers: dict[str, str] | None = None,
        supports_json_schema: bool | None = None,
    ) -> None:
        self.model = model
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._extra_headers = default_headers or {}
        self._supports_json_schema = supports_json_schema

    @property
    def supports_json_schema(self) -> bool:
        """True when the endpoint supports OpenAI-style ``json_schema`` response_format.

        Defaults to True only for the official OpenAI API; other compatible
        endpoints vary in support. Override via the constructor parameter.
        """
        if self._supports_json_schema is not None:
            return self._supports_json_schema
        from urllib.parse import urlparse

        return urlparse(self.base_url).hostname == "api.openai.com"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)
        return headers

    def _build_payload(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | None,
        settings: ModelSettings | None,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message_to_openai(m) for m in messages],
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
        if response_format is not None:
            payload["response_format"] = response_format
        if settings is not None:
            if settings.temperature is not None:
                payload["temperature"] = settings.temperature
            if settings.top_p is not None:
                payload["top_p"] = settings.top_p
            if settings.max_tokens is not None:
                payload["max_tokens"] = settings.max_tokens
            if settings.stop is not None:
                payload["stop"] = settings.stop
            if settings.parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = settings.parallel_tool_calls
            payload.update(settings.extra)
        if stream:
            # Asking for usage in the stream requires opt-in.
            payload.setdefault("stream_options", {"include_usage": True})
        return payload

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AssistantMessage:
        payload = self._build_payload(
            messages, tools, response_format, settings, stream=False
        )
        try:
            response = await self._client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(f"OpenAI request failed: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(
                f"OpenAI returned HTTP {response.status_code}: {response.text}"
            )
        data = response.json()
        return _parse_completion(data)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload = self._build_payload(
            messages, tools, response_format, settings, stream=True
        )

        # Incremental state assembled while we forward deltas to the caller.
        text_buf: list[str] = []
        reasoning_buf: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}  # index -> {id, name, arguments}
        usage = Usage()
        finish_reason: str | None = None

        async with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise ProviderError(
                    f"OpenAI stream returned HTTP {response.status_code}: {body.decode(errors='replace')}"
                )
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[len("data:") :].strip()
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if "usage" in event and event["usage"]:
                    u = event["usage"]
                    usage.input_tokens = u.get("prompt_tokens", 0)
                    usage.output_tokens = u.get("completion_tokens", 0)
                    pdetails = u.get("prompt_tokens_details") or {}
                    usage.cache_read_tokens = pdetails.get("cached_tokens", 0)

                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}

                if text := delta.get("content"):
                    text_buf.append(text)
                    yield StreamChunk(text_delta=text)

                if reasoning := delta.get("reasoning_content"):
                    reasoning_buf.append(reasoning)
                    yield StreamChunk(reasoning_delta=reasoning)

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_calls.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                    yield StreamChunk(
                        tool_call_delta=ToolCallDelta(
                            index=idx,
                            id=tc.get("id"),
                            name=fn.get("name"),
                            arguments_delta=fn.get("arguments"),
                        )
                    )

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

        final = AssistantMessage(
            content="".join(text_buf) or None,
            reasoning_content="".join(reasoning_buf) or None,
            tool_calls=[
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for _, tc in sorted(tool_calls.items())
            ],
            usage=usage,
            finish_reason=finish_reason,
        )
        yield StreamChunk(done=final)


def _parse_completion(data: dict[str, Any]) -> AssistantMessage:
    """Parse a non-streamed completion response."""
    if not data.get("choices"):
        raise ProviderError(f"OpenAI response has no choices: {data}")
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        tool_calls.append(
            ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=fn.get("arguments", ""),
            )
        )
    usage_data = data.get("usage") or {}
    pdetails = usage_data.get("prompt_tokens_details") or {}
    usage = Usage(
        input_tokens=usage_data.get("prompt_tokens", 0),
        output_tokens=usage_data.get("completion_tokens", 0),
        cache_read_tokens=pdetails.get("cached_tokens", 0),
    )
    return AssistantMessage(
        content=msg.get("content"),
        reasoning_content=msg.get("reasoning_content"),
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=choice.get("finish_reason"),
    )
