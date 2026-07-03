"""Anthropic Messages API provider.

Translates lovia transcript entries into the Anthropic Messages API and back.
We talk HTTP directly so the ``anthropic`` SDK is not required.

Conversions worth noting:

* The Anthropic API takes a separate ``system`` parameter, not messages with
  role ``system``. We merge system messages into that parameter.
* ``tool`` role messages become ``user`` messages whose content is a list
  containing a ``tool_result`` block keyed by ``tool_use_id``.
* Assistant tool calls become ``tool_use`` content blocks; we generate the
  ``id`` mapping on the fly.
* ``thinking``/``redacted_thinking`` blocks round-trip through
  :class:`ReasoningEntry` (signature and redacted data in ``metadata``). On
  the official endpoint they are replayed only when the current request
  enables thinking — the API rejects them otherwise. Compatible endpoints
  with thinking on by default (e.g. DeepSeek's ``/anthropic``) always replay.
* Streaming uses Anthropic's SSE event types (``content_block_delta``,
  ``message_delta``, ...) which we translate into :class:`ModelDelta` values
  consumed by the runner.
"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from ..types import JsonObject
from ..exceptions import ProviderError, UserError
from ..transcript import (
    FinishDelta,
    InputEntry,
    TranscriptEntry,
    EntryCompletedDelta,
    ModelDelta,
    AssistantTextEntry,
    ReasoningDelta,
    ReasoningEntry,
    TextDelta,
    ToolCallDelta,
    ToolCallEntry,
    ToolResultEntry,
    UsageDelta,
)
from ..messages import Usage
from ..http_config import resolve_timeout, resolve_trust_env, resolve_verify
from ._content import (
    content_to_anthropic_blocks as _content_to_anthropic_blocks,
    merge_anthropic_blocks as _merge_anthropic_blocks,
    openai_tool_to_anthropic as _openai_tool_to_anthropic,
    text_only as _text_only,
)
from ._http import raise_for_provider_status, raise_for_transport_error
from ._sse import iter_sse_json
from .base import ModelSettings, provider_options

_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
_DEFAULT_VERSION = "2023-06-01"


class AnthropicProvider:
    """Anthropic Messages API adapter."""

    name = "anthropic"
    supports_json_schema = True

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        anthropic_version: str = _DEFAULT_VERSION,
        # Applied when ModelSettings.max_tokens is unset (the API requires the
        # field). 16_384 matches Compaction's default reserve_output_tokens, so
        # the output headroom the context budget reserves is actually usable;
        # every current Claude model allows >= 32k output tokens. For retired
        # 3.x-era models with 4k/8k output caps, pass an explicit lower value.
        default_max_tokens: int = 16_384,
        default_headers: dict[str, str] | None = None,
        trust_env: bool | None = None,
    ) -> None:
        self.model = model
        self.base_url = (
            base_url or os.environ.get("ANTHROPIC_BASE_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = client
        self._owns_client = client is None
        self._timeout = resolve_timeout(timeout)
        self._version = anthropic_version
        self._default_max_tokens = default_max_tokens
        self._extra_headers = dict(default_headers or {})
        self._trust_env = resolve_trust_env(trust_env)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                trust_env=self._trust_env,
                verify=resolve_verify(),
            )
        return self._client

    def context_window(self, model: str) -> int | None:
        return _ANTHROPIC_CONTEXT_WINDOWS.get(model)

    def _using_official_endpoint(self) -> bool:
        return urlparse(self.base_url).hostname == "api.anthropic.com"

    def _check_ready(self) -> None:
        if self._using_official_endpoint() and not self._api_key:
            raise UserError(
                "Anthropic provider requires an API key for api.anthropic.com",
                hint="Set ANTHROPIC_API_KEY or pass api_key=...; use base_url=... for compatible gateways that do not need one.",
            )

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "anthropic-version": self._version,
        }
        if self._api_key:
            headers["x-api-key"] = self._api_key
        for key, value in self._extra_headers.items():
            if key.lower() == "x-api-key" and self._api_key:
                continue
            headers[key] = value
        return headers

    def _build_payload(
        self,
        entries: list[TranscriptEntry],
        tools: list[JsonObject] | None,
        response_format: JsonObject | None,
        settings: ModelSettings | None,
        stream: bool,
    ) -> JsonObject:
        extra = (
            provider_options(settings, self.name, "claude")
            if settings is not None
            else {}
        )
        cache_system = bool(extra.pop("cache_system", False))
        thinking = extra.get("thinking")
        thinking_off = thinking is None or (
            isinstance(thinking, dict) and thinking.get("type") == "disabled"
        )
        # The official API rejects thinking blocks unless the request enables
        # thinking, so strip stale replay state (e.g. the option was turned
        # off mid-session). Compatible endpoints may think by default without
        # the option being set, so only the official endpoint gets the gate.
        replay_thinking = not (self._using_official_endpoint() and thinking_off)
        system_blocks, anthropic_messages = _to_anthropic_messages(
            entries, reasoning_provider=self.name, replay_thinking=replay_thinking
        )
        if cache_system and system_blocks:
            # Mark the system prompt as cacheable (ephemeral 5-minute TTL).
            system_blocks[-1] = {
                **system_blocks[-1],
                "cache_control": {"type": "ephemeral"},
            }

        payload: JsonObject = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": (
                settings.max_tokens
                if settings and settings.max_tokens is not None
                else self._default_max_tokens
            ),
        }
        if system_blocks:
            payload["system"] = system_blocks
        if tools:
            anthropic_tools = [_openai_tool_to_anthropic(t) for t in tools]
            if cache_system and anthropic_tools:
                # Cache the tool definitions too — they typically change less
                # often than the conversation.
                anthropic_tools[-1] = {
                    **anthropic_tools[-1],
                    "cache_control": {"type": "ephemeral"},
                }
            payload["tools"] = anthropic_tools
        if response_format is not None:
            output_config = _response_format_to_output_config(response_format)
            if output_config is not None:
                payload["output_config"] = output_config
        if (
            tools
            and settings is not None
            and settings.parallel_tool_calls is False
            and "tool_choice" not in extra
        ):
            payload["tool_choice"] = {
                "type": "auto",
                "disable_parallel_tool_use": True,
            }
        if stream:
            payload["stream"] = True
        if settings is not None:
            if settings.temperature is not None:
                payload["temperature"] = settings.temperature
            if settings.top_p is not None:
                payload["top_p"] = settings.top_p
            if settings.stop is not None:
                payload["stop_sequences"] = settings.stop
            # Provider-specific extras intentionally win over adapter defaults
            # such as output_config/tool_choice.
            payload.update(extra)
        return payload

    async def stream(
        self,
        entries: list[TranscriptEntry],
        *,
        tools: list[JsonObject] | None = None,
        response_format: JsonObject | None = None,
        settings: ModelSettings | None = None,
    ) -> AsyncIterator[ModelDelta]:
        self._check_ready()
        payload = self._build_payload(
            entries, tools, response_format, settings, stream=True
        )

        # Anthropic streams content blocks by index. We only need to remember
        # id/name per block so we can echo them on every argument delta.
        block_kinds: dict[int, str] = {}
        tool_call_ids: dict[int, str] = {}
        tool_call_names: dict[int, str] = {}
        tool_call_arguments: dict[int, str] = {}
        text_blocks: dict[int, list[str]] = {}
        thinking_blocks: dict[int, list[str]] = {}
        thinking_signatures: dict[int, str] = {}
        redacted_data: dict[int, str] = {}
        usage = Usage()
        stop_reason: str | None = None

        try:
            async with self._http().stream(
                "POST",
                f"{self.base_url}/messages",
                headers=self._headers(),
                json=payload,
            ) as response:
                await raise_for_provider_status(
                    response,
                    vendor="anthropic",
                    model=self.model,
                    label="Anthropic",
                    is_context_overflow=_is_context_overflow,
                )
                async for event in iter_sse_json(response):
                    etype = event.get("type")

                    if etype == "content_block_start":
                        idx = event.get("index", 0)
                        block = event.get("content_block") or {}
                        block_kinds[idx] = block.get("type", "")
                        if block.get("type") == "tool_use":
                            tool_call_ids[idx] = block.get("id", "")
                            tool_call_names[idx] = block.get("name", "")
                            initial_args = (
                                json.dumps(block["input"]) if block.get("input") else ""
                            )
                            tool_call_arguments[idx] = initial_args
                            yield ToolCallDelta(
                                index=idx,
                                call_id=tool_call_ids[idx],
                                name=tool_call_names[idx],
                                arguments=initial_args,
                            )
                        elif block.get("type") == "text":
                            text = block.get("text", "")
                            text_blocks.setdefault(idx, []).append(text)
                            if text:
                                yield TextDelta(text=text)
                        elif block.get("type") == "thinking":
                            thinking = block.get("thinking", "")
                            thinking_blocks.setdefault(idx, []).append(thinking)
                            if thinking:
                                yield ReasoningDelta(text=thinking)
                            if signature := block.get("signature"):
                                thinking_signatures[idx] = signature
                        elif block.get("type") == "redacted_thinking":
                            # Arrives complete; content is encrypted, so there
                            # is nothing to surface as a display delta.
                            redacted_data[idx] = block.get("data", "")
                    elif etype == "content_block_delta":
                        idx = event.get("index", 0)
                        delta = event.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            text = delta.get("text", "")
                            text_blocks.setdefault(idx, []).append(text)
                            yield TextDelta(text=text)
                        elif dtype == "thinking_delta":
                            thinking = delta.get("thinking", "")
                            thinking_blocks.setdefault(idx, []).append(thinking)
                            yield ReasoningDelta(text=thinking)
                        elif dtype == "signature_delta":
                            if signature := delta.get("signature"):
                                thinking_signatures[idx] = signature
                        elif dtype == "input_json_delta":
                            partial = delta.get("partial_json", "")
                            tool_call_arguments[idx] = (
                                tool_call_arguments.get(idx, "") + partial
                            )
                            yield ToolCallDelta(
                                index=idx,
                                call_id=tool_call_ids.get(idx, ""),
                                name=tool_call_names.get(idx, ""),
                                arguments=partial,
                            )
                    elif etype == "content_block_stop":
                        idx = event.get("index", 0)
                        kind = block_kinds.get(idx)
                        if kind == "text":
                            content = "".join(text_blocks.get(idx, []))
                            if content:
                                yield EntryCompletedDelta(
                                    AssistantTextEntry(content=content)
                                )
                        elif kind == "thinking":
                            metadata: JsonObject = {}
                            if signature := thinking_signatures.get(idx):
                                metadata["signature"] = signature
                            yield EntryCompletedDelta(
                                ReasoningEntry(
                                    content="".join(thinking_blocks.get(idx, [])),
                                    provider=self.name,
                                    metadata=metadata,
                                )
                            )
                        elif kind == "redacted_thinking":
                            # Preserved so tool-use turns can replay the block;
                            # dropping it makes the next request invalid.
                            yield EntryCompletedDelta(
                                ReasoningEntry(
                                    content="",
                                    provider=self.name,
                                    metadata={"redacted": redacted_data.get(idx, "")},
                                )
                            )
                        elif kind == "tool_use":
                            yield EntryCompletedDelta(
                                ToolCallEntry(
                                    call_id=tool_call_ids.get(idx, ""),
                                    name=tool_call_names.get(idx, ""),
                                    arguments=tool_call_arguments.get(idx) or "{}",
                                )
                            )
                    elif etype == "message_delta":
                        delta = event.get("delta") or {}
                        stop_reason = delta.get("stop_reason") or stop_reason
                        if u_raw := event.get("usage"):
                            u = u_raw
                            usage.output_tokens = u.get(
                                "output_tokens", usage.output_tokens
                            )
                            if "cache_creation_input_tokens" in u:
                                usage.cache_write_tokens = u[
                                    "cache_creation_input_tokens"
                                ]
                            if "cache_read_input_tokens" in u:
                                usage.cache_read_tokens = u["cache_read_input_tokens"]
                    elif etype == "message_start":
                        message = event.get("message") or {}
                        if u_raw := message.get("usage"):
                            u = u_raw
                            usage.input_tokens = u.get("input_tokens", 0)
                            usage.output_tokens = u.get("output_tokens", 0)
                            usage.cache_write_tokens = u.get(
                                "cache_creation_input_tokens", 0
                            )
                            usage.cache_read_tokens = u.get(
                                "cache_read_input_tokens", 0
                            )
                    elif etype == "message_stop":
                        break
                    elif etype == "error":
                        error = event.get("error") or {}
                        msg = (
                            error.get("message")
                            or event.get("message")
                            or "Anthropic error"
                        )
                        raise ProviderError(
                            f"Anthropic stream error: {msg}",
                            vendor="anthropic",
                            model=self.model,
                            retryable=_is_stream_error_retryable(error),
                        )
        except ProviderError:
            raise
        except httpx.TransportError as exc:
            raise_for_transport_error(
                exc,
                vendor="anthropic",
                model=self.model,
                label="Anthropic",
            )

        # Anthropic's ``input_tokens`` counts only the uncached slice of the
        # prompt; cache reads/writes are reported separately. Normalize to the
        # framework convention (``Usage.input_tokens`` = the full prompt, as
        # OpenAI's ``prompt_tokens`` already is) — otherwise every consumer of
        # the raw number (budgets, the context policy's calibration) sees a
        # prompt that shrinks to near-zero whenever the cache is warm.
        usage.input_tokens += usage.cache_read_tokens + usage.cache_write_tokens
        yield UsageDelta(usage=usage)
        yield FinishDelta(reason=_normalize_stop_reason(stop_reason))


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


def _response_format_to_output_config(
    response_format: JsonObject,
) -> JsonObject | None:
    """Map supported OpenAI response_format shapes to Anthropic output_config."""
    if response_format.get("type") != "json_schema":
        return None
    json_schema = response_format.get("json_schema")
    if not isinstance(json_schema, dict):
        return None
    schema = json_schema.get("schema")
    if not isinstance(schema, dict):
        return None
    return {"format": {"type": "json_schema", "schema": schema}}


def _is_stream_error_retryable(error: JsonObject) -> bool:
    error_type = str(error.get("type") or "").lower()
    message = str(error.get("message") or "").lower()
    text = f"{error_type} {message}"
    return any(
        needle in text
        for needle in ("overloaded", "rate", "timeout", "temporar", "server")
    )


def _append_user_message(out: list[JsonObject], content: list[JsonObject]) -> None:
    """Append Anthropic user content, merging adjacent user messages.

    Anthropic represents tool results as user messages, so this keeps compacted
    history valid without changing the stored transcript entries.
    """
    if out and out[-1].get("role") == "user":
        out[-1]["content"] = _merge_anthropic_blocks(out[-1].get("content"), content)
        return
    out.append({"role": "user", "content": content})


def _to_anthropic_messages(
    entries: list[TranscriptEntry],
    *,
    reasoning_provider: str = "anthropic",
    replay_thinking: bool = True,
) -> tuple[list[JsonObject] | None, list[JsonObject]]:
    """Translate transcript entries into Anthropic's API shape.

    Returns ``(system_blocks, messages)`` where ``system_blocks`` is either
    ``None`` or a list of text blocks suitable for the Anthropic ``system``
    parameter (we use the block form so callers can attach ``cache_control``).

    Only :class:`ReasoningEntry` values written by ``reasoning_provider`` are
    replayed, and only when ``replay_thinking`` is set — the adapter turns it
    off when the current request would be rejected for containing them.
    """
    system_parts: list[str] = []
    out: list[JsonObject] = []
    pending_blocks: list[JsonObject] = []

    def flush_assistant() -> None:
        nonlocal pending_blocks
        if not pending_blocks:
            return
        if all(
            block.get("type") in ("thinking", "redacted_thinking")
            for block in pending_blocks
        ):
            # Compaction may leave provider replay state without its assistant
            # action; Anthropic thinking blocks cannot stand alone either.
            pending_blocks = []
            return
        out.append({"role": "assistant", "content": pending_blocks})
        pending_blocks = []

    for entry in entries:
        if isinstance(entry, InputEntry) and entry.role == "system":
            if entry.content:
                system_parts.append(_text_only(entry.content))
            continue

        if isinstance(entry, ToolResultEntry):
            flush_assistant()
            result_block: JsonObject = {
                "type": "tool_result",
                "tool_use_id": entry.call_id,
                "content": entry.output,
            }
            if entry.is_error:
                result_block["is_error"] = True
            _append_user_message(out, [result_block])
            continue

        if isinstance(entry, ReasoningEntry):
            if entry.provider != reasoning_provider or not replay_thinking:
                continue
            redacted = entry.metadata.get("redacted")
            if isinstance(redacted, str) and redacted:
                pending_blocks.append(
                    {"type": "redacted_thinking", "data": redacted}
                )
                continue
            block: JsonObject = {"type": "thinking", "thinking": entry.content}
            signature = entry.metadata.get("signature")
            if isinstance(signature, str) and signature:
                block["signature"] = signature
            pending_blocks.append(block)
            continue

        if isinstance(entry, AssistantTextEntry):
            pending_blocks.extend(_content_to_anthropic_blocks(entry.content))
            continue

        if isinstance(entry, ToolCallEntry):
            try:
                parsed = json.loads(entry.arguments or "{}")
            except json.JSONDecodeError:
                parsed = {"_raw": entry.arguments}
            pending_blocks.append(
                {
                    "type": "tool_use",
                    "id": entry.call_id,
                    "name": entry.name,
                    "input": parsed,
                }
            )
            continue

        if isinstance(entry, InputEntry):
            flush_assistant()
            _append_user_message(out, _content_to_anthropic_blocks(entry.content))

    flush_assistant()
    system_blocks: list[JsonObject] | None
    if system_parts:
        system_blocks = [{"type": "text", "text": "\n\n".join(system_parts)}]
    else:
        system_blocks = None
    return system_blocks, out


# Anthropic returns 400 with ``invalid_request_error`` and a message like
# "prompt is too long". Some gateway proxies relabel the status; we accept any
# 4xx whose body matches one of the known phrases.
def _is_context_overflow(status: int, body: str) -> bool:
    if status < 400 or status >= 500:
        return False
    lowered = body.lower()
    return (
        "prompt is too long" in lowered
        or "input is too long" in lowered
        or "context window" in lowered
        # "input length and `max_tokens` exceed context limit" (current API)
        or "context limit" in lowered
        # Bedrock-hosted Claude behind gateways
        or "too many total text bytes" in lowered
        or "max_tokens_to_sample" in lowered
        and "exceeds" in lowered
    )


# Context-window table for recent, commonly used Anthropic aliases. Date-pinned
# snapshots and retired Claude 3.x/older 4.x aliases fall back to reactive
# overflow handling.
#
# Values are the *default* windows. The 1M-token variants are gated behind the
# ``context-1m`` beta header, which this adapter does not send by default;
# advertising 1M here would make proactive compaction trigger far too late.
# Users who enable the beta can size their ContextPolicy explicitly.
_ANTHROPIC_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-8": 200_000,
    "claude-opus-4-7": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
}
