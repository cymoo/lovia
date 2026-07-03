"""OpenAI Chat Completions provider.

This adapter speaks the OpenAI Chat Completions HTTP API directly via
``httpx``. It does not depend on the ``openai`` SDK, which keeps the install
footprint small and lets us point at any compatible endpoint (DeepSeek, Qwen,
Kimi, Ollama, vLLM, LM Studio, ...) by setting ``base_url``.
"""

from __future__ import annotations

import os
import re
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from ..types import JsonObject
from ..exceptions import ProviderError, UserError
from ..transcript import (
    FinishDelta,
    TranscriptEntry,
    EntryCompletedDelta,
    ModelDelta,
    InputEntry,
    AssistantTextEntry,
    ReasoningDelta,
    ReasoningEntry,
    TextDelta,
    ToolCallEntry,
    ToolCallDelta,
    ToolResultEntry,
    UsageDelta,
)
from ..messages import Message, ToolCall, Usage
from ..http_config import resolve_timeout, resolve_trust_env, resolve_verify
from ._content import (
    content_to_openai_chat as _content_to_openai,
    merge_openai_chat_content as _merge_openai_content,
)
from ._http import host_matches, raise_for_provider_status, raise_for_transport_error
from ._sse import iter_sse_json
from .base import ModelSettings, provider_options

_DEFAULT_BASE_URL = "https://api.openai.com/v1"

# Hosts that speak the official OpenAI API dialect: strict parameter set
# (``max_completion_tokens``), native ``json_schema`` response_format, no
# ``reasoning_content``. Subdomains match too, which covers the regional
# data-residency hosts (``eu.api.openai.com``). Gateways that merely forward
# to the official API can opt in via the ``official_api`` constructor flag.
_OFFICIAL_HOSTS = ("api.openai.com",)


# ---------------------------------------------------------------------------
# Wire-format serialization (OpenAI Chat Completions schema)
#
# Kept here — not on ``Message`` itself — so the core message type stays
# vendor-neutral. Other providers translate their own way.


def _tool_call_to_openai(tc: ToolCall) -> JsonObject:
    return {
        "id": tc.id,
        "type": "function",
        "function": {"name": tc.name, "arguments": tc.arguments},
    }


def message_to_openai(msg: Message) -> JsonObject:
    """Serialize a :class:`Message` to the OpenAI Chat Completions wire format."""
    out: JsonObject = {"role": msg.role}
    if msg.content is not None:
        out["content"] = _content_to_openai(msg.content)
    if msg.tool_calls:
        out["tool_calls"] = [_tool_call_to_openai(tc) for tc in msg.tool_calls]
    if msg.tool_call_id is not None:
        out["tool_call_id"] = msg.tool_call_id
    if msg.name is not None and msg.role in ("user", "assistant"):
        out["name"] = msg.name
    return out


def _append_input_message(out: list[JsonObject], entry: InputEntry) -> None:
    """Append a system/user entry in OpenAI Chat format.

    Compaction can create adjacent input entries; merging them avoids provider
    quirks while preserving the canonical transcript shape.
    """
    msg = message_to_openai(Message(entry.role, entry.content))
    if out and out[-1].get("role") == entry.role:
        out[-1]["content"] = _merge_openai_content(
            out[-1].get("content"), msg.get("content")
        )
        return
    out.append(msg)


def _assistant_to_openai(
    content: str | None,
    tool_calls: list[ToolCall],
    reasoning_content: str | None,
) -> JsonObject:
    out: JsonObject = {"role": "assistant"}
    if content is not None:
        out["content"] = content
    if reasoning_content is not None:
        out["reasoning_content"] = reasoning_content
    if tool_calls:
        out["tool_calls"] = [_tool_call_to_openai(tc) for tc in tool_calls]
    return out


def entries_to_openai_messages(
    entries: list[TranscriptEntry],
    *,
    reasoning_provider: str = "openai-chat",
    include_reasoning: bool = True,
) -> list[JsonObject]:
    """Serialize transcript entries to OpenAI Chat messages.

    ``include_reasoning`` controls whether :class:`ReasoningEntry` values are
    replayed as ``reasoning_content`` — endpoints disagree on the field (see
    ``_REASONING_REPLAY_DEFAULTS``).
    """

    out: list[JsonObject] = []
    pending_reasoning: str | None = None
    pending_content: str | None = None
    pending_calls: list[ToolCall] = []

    def flush_assistant() -> None:
        nonlocal pending_reasoning, pending_content, pending_calls
        if pending_content is None and not pending_calls and pending_reasoning is None:
            return
        if pending_content is None and not pending_calls:
            # Compaction may leave provider replay state without its assistant
            # action; OpenAI rejects assistant messages that only contain it.
            pending_reasoning = None
            return
        out.append(
            _assistant_to_openai(pending_content, pending_calls, pending_reasoning)
        )
        pending_reasoning = None
        pending_content = None
        pending_calls = []

    for entry in entries:
        if isinstance(entry, InputEntry):
            flush_assistant()
            _append_input_message(out, entry)
        elif isinstance(entry, ReasoningEntry):
            if include_reasoning and entry.provider == reasoning_provider:
                pending_reasoning = (pending_reasoning or "") + entry.content
        elif isinstance(entry, AssistantTextEntry):
            pending_content = (pending_content or "") + entry.content
        elif isinstance(entry, ToolCallEntry):
            pending_calls.append(
                ToolCall(id=entry.call_id, name=entry.name, arguments=entry.arguments)
            )
        elif isinstance(entry, ToolResultEntry):
            flush_assistant()
            out.append(
                message_to_openai(
                    Message(
                        role="tool",
                        content=entry.output,
                        tool_call_id=entry.call_id,
                    )
                )
            )
    flush_assistant()
    return out


class OpenAIChatProvider:
    """OpenAI Chat Completions API adapter.

    Args:
        model: The model identifier sent to the API (e.g. ``"gpt-5.4"``).
        api_key: API key. Defaults to ``$OPENAI_API_KEY``.
        base_url: Override to target an OpenAI-compatible endpoint.
        client: Optional pre-built :class:`httpx.AsyncClient`. If omitted we
            create one per provider instance and reuse it.
        timeout: Request timeout in seconds. Defaults to the
            ``LOVIA_PROVIDER_TIMEOUT`` environment variable, else 60.
        default_headers: Extra headers merged into every request (useful for
            providers that require custom auth headers).
        trust_env: Whether the provider-created HTTP client should honor proxy
            and certificate environment variables. Defaults to ``False`` so an
            ambient proxy setting cannot make the provider require optional
            dependencies such as SOCKS support. Pass a custom client for more
            advanced transport configuration.
        replay_reasoning: Whether ``reasoning_content`` from earlier turns is
            replayed on assistant input messages. ``None`` (default) resolves
            per endpoint host: DeepSeek requires the replay, the official
            OpenAI API rejects the field, and unlisted hosts replay. Pass
            ``True``/``False`` to override for endpoints we guess wrong.
        official_api: Whether the endpoint speaks the official OpenAI API
            dialect (``max_completion_tokens`` instead of ``max_tokens``,
            native ``json_schema``, no ``reasoning_content``). ``None``
            (default) infers from the host. Set ``True`` for gateways that
            forward to the official API; the narrower ``supports_json_schema``
            and ``replay_reasoning`` flags still win where both are given.
            Does not affect the API-key requirement, which follows the real
            host — a keyless gateway keeps working with ``official_api=True``.
    """

    name = "openai-chat"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        default_headers: dict[str, str] | None = None,
        supports_json_schema: bool | None = None,
        trust_env: bool | None = None,
        replay_reasoning: bool | None = None,
        official_api: bool | None = None,
    ) -> None:
        self.model = model
        self.base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or _DEFAULT_BASE_URL
        ).rstrip("/")
        self._host = urlparse(self.base_url).hostname or ""
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = client
        self._owns_client = client is None
        self._timeout = resolve_timeout(timeout)
        self._extra_headers = dict(default_headers or {})
        self._supports_json_schema = supports_json_schema
        self._trust_env = resolve_trust_env(trust_env)
        self._replay_reasoning = replay_reasoning
        self._official_api = official_api

    @property
    def supports_json_schema(self) -> bool:
        """True when the endpoint supports OpenAI-style ``json_schema`` response_format.

        Defaults to True only for the official API dialect (see
        ``official_api``); other compatible endpoints vary in support.
        Override via the constructor parameter.
        """
        if self._supports_json_schema is not None:
            return self._supports_json_schema
        return self._speaks_official_api()

    def _on_official_host(self) -> bool:
        """The endpoint literally is the official API (auth requirements)."""
        return host_matches(self._host, _OFFICIAL_HOSTS)

    def _speaks_official_api(self) -> bool:
        """The endpoint follows the official API dialect (request shape)."""
        if self._official_api is not None:
            return self._official_api
        return self._on_official_host()

    def _should_replay_reasoning(self) -> bool:
        if self._replay_reasoning is not None:
            return self._replay_reasoning
        default = _REASONING_REPLAY_DEFAULTS.get(self._host)
        if default is not None:
            return default
        return not self._speaks_official_api()

    def _check_ready(self) -> None:
        if self._on_official_host() and not self._api_key:
            raise UserError(
                "OpenAI Chat provider requires an API key for api.openai.com",
                hint="Set OPENAI_API_KEY or pass api_key=...; use base_url=... for OpenAI-compatible endpoints that do not need one.",
            )

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

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        for key, value in self._extra_headers.items():
            if key.lower() == "authorization" and self._api_key:
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
        payload: JsonObject = {
            "model": self.model,
            "messages": entries_to_openai_messages(
                entries,
                reasoning_provider=self.name,
                include_reasoning=self._should_replay_reasoning(),
            ),
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
                # Current official models reject the legacy ``max_tokens``
                # ("use 'max_completion_tokens'"), while compatible endpoints
                # mostly accept only the legacy spelling.
                if self._speaks_official_api():
                    payload["max_completion_tokens"] = settings.max_tokens
                else:
                    payload["max_tokens"] = settings.max_tokens
            if settings.stop is not None:
                payload["stop"] = settings.stop
            if settings.parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = settings.parallel_tool_calls
            payload.update(provider_options(settings, "openai", self.name))
        if stream:
            # Asking for usage in the stream requires opt-in.
            payload.setdefault("stream_options", {"include_usage": True})
        # None marks explicit removal (see provider_options), giving users a
        # way to strip adapter defaults for endpoints that reject them.
        return {k: v for k, v in payload.items() if v is not None}

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

        # We only need to remember the per-index tool-call id+name so we can
        # echo them on every argument delta — the runner does the final
        # assembly itself.
        tool_call_ids: dict[int, str] = {}
        tool_call_names: dict[int, str] = {}
        usage = Usage()
        finish_reason: str | None = None
        reasoning_parts: list[str] = []

        try:
            async with self._http().stream(
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
                        reasoning_parts.append(reasoning)
                        yield ReasoningDelta(text=reasoning)

                    for pos, tc in enumerate(delta.get("tool_calls") or []):
                        # Gateways that omit ``index`` (or send it as null)
                        # emit complete calls, so list position keeps parallel
                        # calls in one chunk from collapsing into one slot.
                        idx = tc.get("index")
                        if not isinstance(idx, int):
                            idx = pos
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
                            arguments=fn.get("arguments") or "",
                        )

                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
        except ProviderError:
            raise
        except httpx.TransportError as exc:
            raise_for_transport_error(
                exc,
                vendor="openai",
                model=self.model,
                label="OpenAI Chat",
            )

        if reasoning_parts:
            yield EntryCompletedDelta(
                ReasoningEntry(
                    content="".join(reasoning_parts),
                    provider=self.name,
                )
            )
        yield UsageDelta(usage=usage)
        yield FinishDelta(reason=finish_reason)

    # ----- ContextPolicy hooks ------------------------------------------------

    def context_window(self, model: str) -> int | None:
        window = _OPENAI_CONTEXT_WINDOWS.get(model)
        if window is None:
            # Date-pinned snapshots ("gpt-4.1-2025-04-14") share their
            # alias's window.
            window = _OPENAI_CONTEXT_WINDOWS.get(
                re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
            )
        return window


# Default for replaying ``reasoning_content`` on assistant input messages,
# keyed by endpoint host. Endpoints disagree:
#
# * DeepSeek thinking models *require* it — omitting the field in a tool loop
#   is a 400 ("The reasoning_content in the thinking mode must be passed back
#   to the API"; verified live against deepseek-v4-pro, 2026-07). Kimi K2
#   thinking documents the same requirement.
# * The official OpenAI API neither emits nor accepts the field; that case is
#   handled by the official-dialect check, not this table, so gateways
#   forwarding to the official API inherit it via ``official_api=True``.
#
# Unlisted compatible hosts replay: every known reasoning_content-emitting
# endpoint tolerates or requires the echo. The ``replay_reasoning``
# constructor argument overrides both the table and the dialect default.
_REASONING_REPLAY_DEFAULTS: dict[str, bool] = {
    "api.deepseek.com": True,
}


# OpenAI Chat returns 400 with ``code: context_length_exceeded`` (or a message
# containing that phrase). We accept a broader set of substrings because gateway
# proxies and OpenAI-compatible endpoints (DeepSeek, Qwen, modal-hosted models,
# vLLM, ...) phrase the same condition differently. The cost of a false positive
# is just an extra reactive compaction attempt, so we err on the side of
# matching.
_OVERFLOW_NEEDLES = (
    "context_length_exceeded",
    "context length",  # "the model's context length is only N"
    "context window",  # some vendors
    "context limit",  # "input length and max_tokens exceed context limit"
    "prompt is too long",
    "input is too long",
    "input length",  # "maximum input length of N"
    "reduce the length of the input",
    "reduce the length of the messages",
    "too many tokens",
    "token count exceeds",  # Gemini-compatible gateways
    "exceeds the maximum number of tokens",
    "too many total text bytes",  # Bedrock-hosted models behind proxies
    "request too large",
)


def _is_context_overflow(status: int, body: str) -> bool:
    if status not in (400, 413):
        return False
    lowered = body.lower()
    if any(needle in lowered for needle in _OVERFLOW_NEEDLES):
        return True
    # ``"string too long"`` shows up in unrelated validation errors too;
    # only treat it as overflow when the body also mentions context.
    if "string too long" in lowered and "context" in lowered:
        return True
    return False


# Context-window table for recent, commonly used OpenAI GPT model aliases
# (their date-pinned snapshots resolve via suffix stripping). Keep this
# intentionally small: o-series, retired models, and niche aliases can fall
# back to reactive overflow handling.
_OPENAI_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4.1": 1_047_576,
    "gpt-5": 400_000,
    "gpt-5.5": 1_050_000,
    "gpt-5.5-pro": 1_050_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.2": 400_000,
    "gpt-5.2-pro": 400_000,
}
