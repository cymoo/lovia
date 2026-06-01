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
* ``previous_response_id`` (for stateful conversations stored on OpenAI's
  side) can be passed through ``provider_options["openai-responses"]``.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

import httpx

from ..exceptions import ProviderError
from ..items import (
    FinishDelta,
    InputMessageItem,
    Item,
    ItemCompletedDelta,
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
from ._content import (
    content_to_responses_input as _content_to_input_blocks,
    openai_chat_tool_to_responses as _openai_chat_tool_to_responses,
)
from ._http import raise_for_provider_status
from ._sse import iter_sse_json
from .base import ModelSettings, provider_options


_DEFAULT_BASE_URL = "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Item → Responses-API input translation
#
# The Responses API takes a typed list of items, very close to lovia's
# Item model — the per-type shape just differs in field names.


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
            if it.provider != "openai-responses":
                continue
            entry = {"type": "reasoning", "summary": []}
            if it.id:
                entry["id"] = it.id
            encrypted = it.metadata.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted:
                entry["encrypted_content"] = encrypted
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


# ---------------------------------------------------------------------------
# Provider


class OpenAIResponsesProvider:
    """OpenAI Responses API adapter."""

    name = "openai-responses"
    supports_json_schema = True

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
            base_url or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None
        self._extra_headers = dict(extra_headers or {})
        # ``store=False`` is the default because lovia already owns the
        # transcript via the Session/Checkpointer stack. Flip it to ``True``
        # if you want to drive conversations purely via ``previous_response_id``.
        self.store = store

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

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
                payload["text"] = {
                    "format": {"type": "json_schema", **fmt["json_schema"]}
                }
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
            payload.update(provider_options(settings, self.name, "responses"))
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
        fn_call_arguments: dict[int, str] = {}
        reasoning_text: dict[int, list[str]] = {}
        usage = Usage()
        finish_reason: str | None = None

        async with self._client.stream(
            "POST",
            f"{self.base_url}/responses",
            headers=self._headers(),
            json=payload,
        ) as response:
            # Reuse the chat-completions overflow detector — the Responses
            # API surfaces the same ``context_length_exceeded`` signal.
            from .openai_chat import _is_context_overflow

            await raise_for_provider_status(
                response,
                vendor="openai",
                model=self.model,
                label="OpenAI Responses",
                is_context_overflow=_is_context_overflow,
            )
            async for evt in iter_sse_json(response):
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
                        idx = evt.get("output_index", 0)
                        reasoning_text.setdefault(idx, []).append(chunk)
                        yield ReasoningDelta(text=chunk)
                elif etype == "response.output_item.added":
                    item = evt.get("item") or {}
                    if item.get("type") == "function_call":
                        idx = evt.get("output_index", 0)
                        fn_call_ids[idx] = item.get("call_id", "")
                        fn_call_names[idx] = item.get("name", "")
                        yield ToolCallDelta(
                            index=idx,
                            call_id=fn_call_ids[idx],
                            name=fn_call_names[idx],
                            arguments="",
                        )
                elif etype == "response.function_call_arguments.delta":
                    idx = evt.get("output_index", 0)
                    chunk = evt.get("delta", "")
                    fn_call_arguments[idx] = fn_call_arguments.get(idx, "") + chunk
                    yield ToolCallDelta(
                        index=idx,
                        call_id=fn_call_ids.get(idx, ""),
                        name=fn_call_names.get(idx, ""),
                        arguments=chunk,
                    )
                elif etype == "response.function_call_arguments.done":
                    idx = evt.get("output_index", 0)
                    args = evt.get("arguments", "")
                    seen = fn_call_arguments.get(idx, "")
                    if args and args != seen:
                        chunk = args[len(seen) :] if args.startswith(seen) else args
                        fn_call_arguments[idx] = seen + chunk
                        yield ToolCallDelta(
                            index=idx,
                            call_id=fn_call_ids.get(idx, ""),
                            name=fn_call_names.get(idx, ""),
                            arguments=chunk,
                        )
                elif etype == "response.output_item.done":
                    item = evt.get("item") or {}
                    if item.get("type") == "function_call":
                        idx = evt.get("output_index", 0)
                        if idx not in fn_call_ids:
                            fn_call_ids[idx] = item.get("call_id", "")
                            fn_call_names[idx] = item.get("name", "")
                            yield ToolCallDelta(
                                index=idx,
                                call_id=fn_call_ids[idx],
                                name=fn_call_names[idx],
                                arguments="",
                            )
                        args = item.get("arguments", "")
                        seen = fn_call_arguments.get(idx, "")
                        if args and args != seen:
                            chunk = args[len(seen) :] if args.startswith(seen) else args
                            fn_call_arguments[idx] = seen + chunk
                            yield ToolCallDelta(
                                index=idx,
                                call_id=fn_call_ids.get(idx, ""),
                                name=fn_call_names.get(idx, ""),
                                arguments=chunk,
                            )
                        yield ItemCompletedDelta(
                            ToolCallItem(
                                call_id=item.get("call_id", fn_call_ids.get(idx, "")),
                                name=item.get("name", fn_call_names.get(idx, "")),
                                arguments=(
                                    item.get("arguments")
                                    or fn_call_arguments.get(idx)
                                    or "{}"
                                ),
                            )
                        )
                    elif item.get("type") == "message":
                        content = _message_output_text(item)
                        if content:
                            yield ItemCompletedDelta(
                                MessageOutputItem(
                                    id=item.get("id"),
                                    content=content,
                                )
                            )
                    elif item.get("type") == "reasoning":
                        idx = evt.get("output_index", 0)
                        metadata: dict[str, Any] = {}
                        encrypted = item.get("encrypted_content")
                        if isinstance(encrypted, str) and encrypted:
                            metadata["encrypted_content"] = encrypted
                        content = _reasoning_text(item) or "".join(
                            reasoning_text.get(idx, [])
                        )
                        yield ItemCompletedDelta(
                            ReasoningItem(
                                id=item.get("id"),
                                content=content,
                                provider=self.name,
                                metadata=metadata,
                            )
                        )
                elif etype == "response.completed":
                    resp = evt.get("response") or {}
                    u = resp.get("usage") or {}
                    usage = Usage(
                        input_tokens=u.get("input_tokens", 0),
                        output_tokens=u.get("output_tokens", 0),
                        cache_read_tokens=(u.get("input_tokens_details") or {}).get(
                            "cached_tokens", 0
                        ),
                    )
                    # ``status`` is the closest analogue to a finish reason.
                    finish_reason = resp.get("status") or "completed"
                elif etype in ("response.failed", "error"):
                    msg = (
                        evt.get("error", {}).get("message")
                        or evt.get("message")
                        or "Responses API error"
                    )
                    raise ProviderError(
                        f"OpenAI Responses error: {msg}",
                        vendor="openai",
                        model=self.model,
                        retryable=_is_stream_error_retryable(evt),
                    )

        yield UsageDelta(usage=usage)
        yield FinishDelta(reason=finish_reason or "stop")


def _message_output_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in item.get("content") or []:
        if block.get("type") in ("output_text", "text"):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _reasoning_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for summary in item.get("summary") or []:
        text = summary.get("text") if isinstance(summary, dict) else None
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _is_stream_error_retryable(event: dict[str, Any]) -> bool:
    error = event.get("error") or {}
    error_type = str(error.get("type") or event.get("type") or "").lower()
    code = str(error.get("code") or event.get("code") or "").lower()
    message = str(error.get("message") or event.get("message") or "").lower()
    text = " ".join((error_type, code, message))
    return any(
        needle in text
        for needle in ("rate", "timeout", "overloaded", "server", "temporar")
    )
