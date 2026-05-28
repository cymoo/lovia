"""OpenAI Chat Completions provider.

This adapter speaks the OpenAI Chat Completions HTTP API directly via
``httpx``. It does not depend on the ``openai`` SDK, which keeps the install
footprint small and lets us point at any compatible endpoint (DeepSeek, Qwen,
Kimi, Ollama, vLLM, LM Studio, ...) by setting ``base_url``.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

import httpx

from ..items import (
    FinishDelta,
    Item,
    ItemDelta,
    ReasoningDelta,
    TextDelta,
    ToolCallDelta,
    UsageDelta,
    items_to_chat_messages,
)
from ..messages import ChatMessage, ToolCall, Usage
from ._content import content_to_openai_chat as _content_to_openai
from ._http import raise_for_provider_status
from ._sse import iter_sse_json
from .base import ModelSettings


_DEFAULT_BASE_URL = "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Wire-format serialization (OpenAI Chat Completions schema)
#
# Kept here — not on ``ChatMessage`` itself — so the core message type stays
# vendor-neutral. Other providers translate their own way.


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

    async def stream(
        self,
        input: list[Item],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ItemDelta]:
        # Chat Completions speaks ChatMessage on the wire; flatten the
        # vendor-neutral Item list to messages here. This is lossy for
        # ReasoningItem ids and server-tool items, but Chat Completions
        # cannot represent those anyway.
        messages = items_to_chat_messages(input)
        payload = self._build_payload(
            messages, tools, response_format, settings, stream=True
        )

        # We only need to remember the per-index tool-call id+name so we can
        # echo them on every argument delta — the runner does the final
        # assembly itself.
        tool_call_ids: dict[int, str] = {}
        tool_call_names: dict[int, str] = {}
        usage = Usage()
        finish_reason: str | None = None

        async with self._client.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        ) as response:
            await raise_for_provider_status(
                response,
                vendor="openai",
                model=self.model,
                label="OpenAI Chat",
                is_context_overflow=_is_context_overflow,
            )
            async for event in iter_sse_json(response):
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
                    yield TextDelta(text=text)

                if reasoning := delta.get("reasoning_content"):
                    yield ReasoningDelta(text=reasoning)

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    if tc.get("id"):
                        tool_call_ids[idx] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        tool_call_names[idx] = fn["name"]
                    # Echo the id/name we've seen so far on every delta so
                    # downstream consumers don't need to track them.
                    yield ToolCallDelta(
                        index=idx,
                        call_id=tool_call_ids.get(idx, ""),
                        name=tool_call_names.get(idx, ""),
                        arguments=fn.get("arguments", ""),
                    )

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

        yield UsageDelta(usage=usage)
        yield FinishDelta(reason=finish_reason)

    # ----- ContextPolicy hooks ------------------------------------------------

    def context_window(self, model: str) -> int | None:
        return _OPENAI_CONTEXT_WINDOWS.get(model)


# OpenAI Chat returns 400 with ``code: context_length_exceeded`` (or a message
# containing that phrase). We accept the substring match too because gateway
# proxies sometimes drop the structured ``code`` field.
def _is_context_overflow(status: int, body: str) -> bool:
    if status not in (400, 413):
        return False
    lowered = body.lower()
    return (
        "context_length_exceeded" in lowered
        or "maximum context length" in lowered
        or "prompt is too long" in lowered
        or "string too long" in lowered
        and "context" in lowered
    )


# Conservative context-window table for the most common OpenAI / OpenAI-compatible
# models. Used by ``ContextPolicy`` to size proactive compaction. Unknown models
# fall back to reactive overflow handling — keeping this short on purpose.
_OPENAI_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4.1-nano": 1_000_000,
    "gpt-5": 400_000,
    "gpt-5-mini": 400_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
}
