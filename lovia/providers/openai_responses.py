"""OpenAI Responses API provider.

Targets ``POST /v1/responses`` — the next-generation OpenAI endpoint that
treats a conversation as a list of typed *items* rather than role-based
chat messages. Compared with :mod:`lovia.providers.openai_chat` this
adapter brings two big wins:

* **Reasoning items survive round trips.** o-series ``reasoning`` items
  (with their opaque ``encrypted_content`` blob and stable id) are
  forwarded verbatim, which is required for multi-turn agentic flows
  on o-series models.
* **Function tools.** Function-call items are streamed as
  :class:`ToolCallDelta`, just like the Chat Completions adapter.

The adapter speaks raw HTTP via ``httpx`` (no ``openai`` SDK).

Notes / limitations:

* This is a *streaming-only* adapter; non-streaming clients can buffer
  the deltas.
* Server-side tools (``web_search``, ``file_search``,
  ``code_interpreter``) are not surfaced as distinct items yet — they
  are best invoked through the Responses-native flow once the type
  family grows a dedicated server-tool delta.
* ``previous_response_id`` (for stateful conversations stored on
  OpenAI's side) can be passed through :attr:`ModelSettings.extra`.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

from ..content import ImageBlock, TextBlock
from ..exceptions import ContextOverflowError, ProviderError
from ..items import (
    FinishDelta,
    InputMessageItem,
    Item,
    ItemDelta,
    MessageOutputItem,
    ReasoningDelta,
    ReasoningItem,
    TextDelta,
    ToolCallDelta,
    ToolCallItem,
    ToolCallOutputItem,
    UsageDelta,
)
from ..messages import Usage
from .base import ModelSettings


_DEFAULT_BASE_URL = "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Item → Responses-API input translation
#
# The Responses API takes a typed list of items, very close to lovia's
# Item model — the per-type shape just differs in field names.


def _content_to_input_blocks(content: str | list[Any]) -> list[dict[str, Any]]:
    """Turn user/system content into Responses ``input_text`` / ``input_image`` blocks."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            out.append({"type": "input_text", "text": block.text})
        elif isinstance(block, ImageBlock):
            url = (
                block.url
                if block.url is not None
                else f"data:{block.mime_type};base64,{block.data}"
            )
            entry: dict[str, Any] = {"type": "input_image", "image_url": url}
            if block.detail is not None:
                entry["detail"] = block.detail
            out.append(entry)
        else:  # pragma: no cover - exhaustiveness guard
            raise TypeError(f"Unsupported content block: {block!r}")
    return out


def _items_to_responses_input(items: list[Item]) -> list[dict[str, Any]]:
    """Translate lovia Items to the Responses API ``input`` array."""
    out: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, InputMessageItem):
            out.append(
                {
                    "type": "message",
                    "role": it.role,
                    "content": _content_to_input_blocks(it.content),
                }
            )
        elif isinstance(it, MessageOutputItem):
            entry = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": it.content}],
            }
            if it.id:
                entry["id"] = it.id
            out.append(entry)
        elif isinstance(it, ReasoningItem):
            # Pass through verbatim. ``content`` for o-series Responses is
            # the opaque encrypted blob; for other providers it's plain text
            # which the Responses API will reject — callers must only feed
            # reasoning items that originated from Responses.
            entry = {"type": "reasoning", "summary": []}
            if it.id:
                entry["id"] = it.id
            if it.content:
                entry["encrypted_content"] = it.content
            out.append(entry)
        elif isinstance(it, ToolCallItem):
            out.append(
                {
                    "type": "function_call",
                    "call_id": it.call_id,
                    "name": it.name,
                    "arguments": it.arguments,
                }
            )
        elif isinstance(it, ToolCallOutputItem):
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": it.call_id,
                    "output": it.output,
                }
            )
    return out


def _openai_chat_tool_to_responses(tool: dict[str, Any]) -> dict[str, Any]:
    """Flatten an OpenAI Chat tool schema to the Responses tool shape.

    Chat: ``{type:"function", function:{name, description, parameters}}``
    Responses: ``{type:"function", name, description, parameters}``
    """
    if tool.get("type") != "function":
        # Built-in tools (web_search, file_search, code_interpreter) are
        # already in the Responses format.
        return tool
    fn = tool.get("function", {})
    out: dict[str, Any] = {"type": "function", "name": fn["name"]}
    if "description" in fn:
        out["description"] = fn["description"]
    if "parameters" in fn:
        out["parameters"] = fn["parameters"]
    return out


# ---------------------------------------------------------------------------
# Provider


class OpenAIResponsesProvider:
    """OpenAI Responses API adapter."""

    name = "openai-responses"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
        extra_headers: dict[str, str] | None = None,
        store: bool = False,
    ) -> None:
        self.model = model
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._extra_headers = dict(extra_headers or {})
        # ``store=False`` is the default because lovia already owns the
        # transcript via the Session/Checkpointer stack. Flip it to ``True``
        # if you want to drive conversations purely via ``previous_response_id``.
        self.store = store

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        headers.update(self._extra_headers)
        return headers

    def context_window(self, model: str) -> int | None:
        # Reuse the chat-completions table — the same model identifiers are
        # accepted by the Responses API.
        from .openai_chat import _OPENAI_CONTEXT_WINDOWS

        return _OPENAI_CONTEXT_WINDOWS.get(model)

    def _build_payload(
        self,
        items: list[Item],
        tools: list[dict[str, Any]] | None,
        response_format: dict[str, Any] | None,
        settings: ModelSettings | None,
        *,
        stream: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": _items_to_responses_input(items),
            "stream": stream,
            "store": self.store,
        }
        # Ask OpenAI to echo encrypted reasoning so we can persist it
        # locally and replay it on the next turn (required for o-series
        # multi-turn agentic flows when ``store=False``).
        payload["include"] = ["reasoning.encrypted_content"]
        if tools:
            payload["tools"] = [_openai_chat_tool_to_responses(t) for t in tools]
        if response_format is not None:
            # Responses uses ``text.format`` rather than ``response_format``.
            fmt = response_format
            if fmt.get("type") == "json_schema" and "json_schema" in fmt:
                payload["text"] = {"format": {"type": "json_schema", **fmt["json_schema"]}}
            else:
                payload["text"] = {"format": fmt}
        if settings is not None:
            if settings.temperature is not None:
                payload["temperature"] = settings.temperature
            if settings.top_p is not None:
                payload["top_p"] = settings.top_p
            if settings.max_tokens is not None:
                payload["max_output_tokens"] = settings.max_tokens
            if settings.parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = settings.parallel_tool_calls
            # Anything else (reasoning effort, previous_response_id, ...)
            # rides through ``extra``.
            payload.update(settings.extra)
        return payload

    async def stream(
        self,
        input: list[Item],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ItemDelta]:
        payload = self._build_payload(
            input, tools, response_format, settings, stream=True
        )

        # Per-output-index bookkeeping. The Responses streaming protocol
        # keys deltas by ``output_index``; we keep one slot per function
        # call so we can echo (call_id, name) on every argument delta.
        fn_call_ids: dict[int, str] = {}
        fn_call_names: dict[int, str] = {}
        usage = Usage()
        finish_reason: str | None = None

        async with self._client.stream(
            "POST",
            f"{self.base_url}/responses",
            headers=self._headers(),
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                text = body.decode(errors="replace")
                # Reuse the chat-completions overflow detector — the Responses
                # API surfaces the same ``context_length_exceeded`` signal.
                from .openai_chat import _is_context_overflow

                if _is_context_overflow(response.status_code, text):
                    raise ContextOverflowError(
                        f"OpenAI Responses: prompt exceeds the model's context window: {text}"
                    )
                raise ProviderError(
                    f"OpenAI Responses stream returned HTTP {response.status_code}: "
                    f"{text}"
                )
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    evt = json.loads(data)
                except json.JSONDecodeError:
                    continue

                etype = evt.get("type", "")

                if etype == "response.output_text.delta":
                    chunk = evt.get("delta", "")
                    if chunk:
                        yield TextDelta(text=chunk)
                elif etype in (
                    "response.reasoning_summary_text.delta",
                    "response.reasoning_text.delta",
                ):
                    chunk = evt.get("delta", "")
                    if chunk:
                        yield ReasoningDelta(text=chunk)
                elif etype == "response.output_item.added":
                    item = evt.get("item") or {}
                    if item.get("type") == "function_call":
                        idx = evt.get("output_index", 0)
                        fn_call_ids[idx] = item.get("call_id", "")
                        fn_call_names[idx] = item.get("name", "")
                elif etype == "response.function_call_arguments.delta":
                    idx = evt.get("output_index", 0)
                    chunk = evt.get("delta", "")
                    yield ToolCallDelta(
                        index=idx,
                        call_id=fn_call_ids.get(idx, ""),
                        name=fn_call_names.get(idx, ""),
                        arguments=chunk,
                    )
                elif etype == "response.completed":
                    resp = evt.get("response") or {}
                    u = resp.get("usage") or {}
                    usage = Usage(
                        input_tokens=u.get("input_tokens", 0),
                        output_tokens=u.get("output_tokens", 0),
                    )
                    # ``status`` is the closest analogue to a finish reason.
                    finish_reason = resp.get("status") or "completed"
                elif etype in ("response.failed", "error"):
                    msg = (
                        evt.get("error", {}).get("message")
                        or evt.get("message")
                        or "Responses API error"
                    )
                    raise ProviderError(f"OpenAI Responses error: {msg}")

        yield UsageDelta(usage=usage)
        yield FinishDelta(reason=finish_reason or "stop")
