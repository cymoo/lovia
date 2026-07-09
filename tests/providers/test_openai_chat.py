"""Tests for the OpenAI Chat Completions provider adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from lovia import Agent, FilePart, ImagePart, Runner, TextPart, events
from lovia.exceptions import ContextOverflowError, ProviderError, UserError
from lovia.transcript import (
    FinishDelta,
    EntryCompletedDelta,
    InputEntry,
    ModelDelta,
    AssistantTextEntry,
    ReasoningDelta,
    ReasoningEntry,
    TextDelta,
    ToolCallEntry,
    ToolCallDelta,
    ToolResultEntry,
    UsageDelta,
)
from lovia.messages import Message, ToolCall
from lovia.providers.base import ModelSettings
from lovia.providers.openai_chat import (
    OpenAIChatProvider,
    _is_context_overflow,
    entries_to_openai_messages,
    message_to_openai,
)

from tests.scripted_provider import ScriptedProvider, text


def _sse(events: list[dict[str, Any]]) -> bytes:
    lines: list[str] = []
    for evt in events:
        lines.append("event: chat.completion.chunk")
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return "\n".join(lines).encode()


def _sse_truncated(events: list[dict[str, Any]]) -> bytes:
    """Like :func:`_sse` but with no ``[DONE]`` terminator — a cut-off stream."""
    lines: list[str] = []
    for evt in events:
        lines.append("event: chat.completion.chunk")
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
    return "\n".join(lines).encode()


async def _collect(stream: Any) -> list[ModelDelta]:
    out: list[ModelDelta] = []
    async for delta in stream:
        out.append(delta)
    return out


def _deltas(deltas: list[ModelDelta], cls: type[Any]) -> Iterator[Any]:
    return (delta for delta in deltas if isinstance(delta, cls))


def test_message_to_openai_serializes_multimodal_and_tool_fields() -> None:
    msg = Message(
        role="assistant",
        content=[
            TextPart("look"),
            ImagePart(url="https://example.test/img.png", detail="low"),
        ],
        tool_calls=[ToolCall(id="c1", name="lookup", arguments='{"q":"x"}')],
        name="agent",
    )

    out = message_to_openai(msg)

    assert out == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "look"},
            {
                "type": "image_url",
                "image_url": {"url": "https://example.test/img.png", "detail": "low"},
            },
        ],
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"x"}'},
            }
        ],
        "name": "agent",
    }


def test_message_to_openai_serializes_inline_file_blocks() -> None:
    msg = Message(
        role="user",
        content=[
            TextPart("summarize"),
            FilePart(data="cGRm", mime_type="application/pdf", filename="doc.pdf"),
        ],
    )

    out = message_to_openai(msg)

    assert out["content"] == [
        {"type": "text", "text": "summarize"},
        {
            "type": "file",
            "file": {
                "file_data": "data:application/pdf;base64,cGRm",
                "filename": "doc.pdf",
            },
        },
    ]


def test_message_to_openai_serializes_tool_result_messages() -> None:
    msg = Message(role="tool", content="42", tool_call_id="call_1", name="ignored")

    assert message_to_openai(msg) == {
        "role": "tool",
        "content": "42",
        "tool_call_id": "call_1",
    }


def test_message_to_openai_rejects_file_url_blocks() -> None:
    msg = Message(
        role="user",
        content=[FilePart.from_url("https://example.test/doc.pdf")],
    )

    with pytest.raises(UserError, match="does not support FilePart URL"):
        message_to_openai(msg)


def test_entries_to_openai_messages_flushes_assistant_entries_in_order() -> None:
    out = entries_to_openai_messages(
        [
            InputEntry(role="user", content="first"),
            ReasoningEntry(content="ignored", provider="other-provider"),
            ReasoningEntry(content="think", provider="openai-chat"),
            AssistantTextEntry(content="hel"),
            AssistantTextEntry(content="lo"),
            ToolCallEntry(call_id="call_1", name="add", arguments='{"a":1}'),
            ToolResultEntry(call_id="call_1", output="1"),
            AssistantTextEntry(content="done"),
            InputEntry(role="user", content="next"),
        ]
    )

    assert out == [
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "hello",
            "reasoning_content": "think",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a":1}'},
                }
            ],
        },
        {"role": "tool", "content": "1", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "next"},
    ]


def test_entries_to_openai_messages_drops_orphan_reasoning() -> None:
    out = entries_to_openai_messages(
        [
            InputEntry(role="user", content="before"),
            ReasoningEntry(content="stale", provider="openai-chat"),
            InputEntry(role="user", content="after"),
        ]
    )

    assert out == [{"role": "user", "content": "before\n\nafter"}]


def test_entries_to_openai_messages_keeps_reasoning_with_tool_call() -> None:
    out = entries_to_openai_messages(
        [
            ReasoningEntry(content="think", provider="openai-chat"),
            ToolCallEntry(call_id="call_1", name="add", arguments='{"a":1}'),
        ]
    )

    assert out == [
        {
            "role": "assistant",
            "reasoning_content": "think",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a":1}'},
                }
            ],
        }
    ]


def test_entries_to_openai_messages_can_exclude_reasoning() -> None:
    out = entries_to_openai_messages(
        [
            ReasoningEntry(content="think", provider="openai-chat"),
            ToolCallEntry(call_id="call_1", name="add", arguments='{"a":1}'),
        ],
        include_reasoning=False,
    )

    assert out == [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "add", "arguments": '{"a":1}'},
                }
            ],
        }
    ]


def _reasoning_replayed(provider: OpenAIChatProvider) -> bool:
    payload = provider._build_payload(
        [
            InputEntry(role="user", content="hi"),
            ReasoningEntry(content="think", provider="openai-chat"),
            AssistantTextEntry(content="ok"),
            InputEntry(role="user", content="next"),
        ],
        tools=None,
        response_format=None,
        settings=None,
        stream=True,
    )
    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    return "reasoning_content" in assistant


def test_reasoning_replay_defaults_per_endpoint_host() -> None:
    # DeepSeek requires the echo; the official API rejects the field;
    # unknown compatible endpoints default to replaying.
    assert _reasoning_replayed(
        OpenAIChatProvider(model="m", api_key="k", base_url="https://api.deepseek.com")
    )
    assert not _reasoning_replayed(
        OpenAIChatProvider(model="m", api_key="k", base_url="https://api.openai.com/v1")
    )
    assert _reasoning_replayed(
        OpenAIChatProvider(model="m", api_key="k", base_url="https://example.test/v1")
    )


def test_reasoning_replay_explicit_override_beats_host_default() -> None:
    assert not _reasoning_replayed(
        OpenAIChatProvider(
            model="m",
            api_key="k",
            base_url="https://api.deepseek.com",
            replay_reasoning=False,
        )
    )
    assert _reasoning_replayed(
        OpenAIChatProvider(
            model="m",
            api_key="k",
            base_url="https://api.openai.com/v1",
            replay_reasoning=True,
        )
    )


def test_entries_to_openai_messages_coalesces_adjacent_user_entries() -> None:
    out = entries_to_openai_messages(
        [
            InputEntry(role="user", content="one"),
            InputEntry(role="user", content="two"),
        ]
    )

    assert out == [{"role": "user", "content": "one\n\ntwo"}]


def test_build_payload_skips_compacted_reasoning_only_turn() -> None:
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test")

    payload = provider._build_payload(
        [
            InputEntry(role="user", content="[Context summary]\n\nold state"),
            ReasoningEntry(content="orphaned replay state", provider="openai-chat"),
            InputEntry(role="user", content="continue"),
        ],
        tools=None,
        response_format=None,
        settings=ModelSettings(),
        stream=True,
    )

    assert payload["messages"] == [
        {
            "role": "user",
            "content": "[Context summary]\n\nold state\n\ncontinue",
        }
    ]


def test_build_payload_maps_settings_and_stream_options() -> None:
    provider = OpenAIChatProvider(
        model="gpt-5", api_key="sk-test", base_url="https://example.test/v1"
    )

    payload = provider._build_payload(
        [InputEntry(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "f"}}],
        response_format={"type": "json_object"},
        settings=ModelSettings(
            temperature=0,
            top_p=0.5,
            max_tokens=50,
            stop=["END"],
            parallel_tool_calls=False,
            provider_options={"openai-chat": {"seed": 1}},
        ),
        stream=True,
    )

    assert payload["model"] == "gpt-5"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["tools"] == [{"type": "function", "function": {"name": "f"}}]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0
    assert payload["top_p"] == 0.5
    assert payload["max_tokens"] == 50
    assert payload["stop"] == ["END"]
    assert payload["parallel_tool_calls"] is False
    assert payload["seed"] == 1


def test_build_payload_uses_max_completion_tokens_on_official_endpoint() -> None:
    def payload_for(base_url: str) -> dict[str, Any]:
        provider = OpenAIChatProvider(
            model="gpt-5", api_key="sk-test", base_url=base_url
        )
        return provider._build_payload(
            [InputEntry(role="user", content="hi")],
            tools=None,
            response_format=None,
            settings=ModelSettings(max_tokens=50),
            stream=True,
        )

    official = payload_for("https://api.openai.com/v1")
    assert official["max_completion_tokens"] == 50
    assert "max_tokens" not in official

    compatible = payload_for("https://api.deepseek.com")
    assert compatible["max_tokens"] == 50
    assert "max_completion_tokens" not in compatible


def test_provider_options_canonical_key_beats_alias() -> None:
    provider = OpenAIChatProvider(
        model="gpt-5", api_key="sk-test", base_url="https://example.test/v1"
    )

    payload = provider._build_payload(
        [InputEntry(role="user", content="hi")],
        tools=None,
        response_format=None,
        settings=ModelSettings(
            provider_options={"openai": {"seed": 2}, "openai-chat": {"seed": 1}}
        ),
        stream=True,
    )

    assert payload["seed"] == 1


def test_entries_to_openai_messages_merges_adjacent_multimodal_user_entries() -> None:
    out = entries_to_openai_messages(
        [
            InputEntry(
                role="user",
                content=[TextPart("look"), ImagePart(url="https://x/y.png")],
            ),
            InputEntry(role="user", content="more"),
        ]
    )

    assert out == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
                {"type": "text", "text": "\n\n"},
                {"type": "text", "text": "more"},
            ],
        }
    ]


@pytest.mark.asyncio
async def test_aclose_closes_owned_client_and_allows_reuse() -> None:
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test")

    first = provider._http()
    await provider.aclose()

    assert first.is_closed
    second = provider._http()
    assert second is not first
    assert not second.is_closed
    await provider.aclose()


@pytest.mark.asyncio
async def test_aclose_leaves_injected_client_open() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    await provider.aclose()

    assert not client.is_closed
    await client.aclose()


def test_headers_keep_explicit_api_key_when_extra_headers_overlap() -> None:
    provider = OpenAIChatProvider(
        model="gpt-5",
        api_key="real-key",
        default_headers={"Authorization": "Bearer wrong-key", "X-Test": "1"},
    )

    headers = provider._headers()

    assert headers["Authorization"] == "Bearer real-key"
    assert headers["X-Test"] == "1"


@pytest.mark.asyncio
async def test_created_client_ignores_ambient_socks_proxy(monkeypatch: Any) -> None:
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7897")
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test")

    provider._http()
    await provider.aclose()


@pytest.mark.asyncio
async def test_chat_stream_parses_text_reasoning_tool_usage_and_finish() -> None:
    captured: dict[str, Any] = {}
    body = _sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "content": "hel",
                            "reasoning_content": "think",
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "content": "lo",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"x"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            {
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
                "choices": [],
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, content=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(
        provider.stream(
            [InputEntry(role="user", content="hi")],
            settings=ModelSettings(max_tokens=10),
        )
    )

    assert captured["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert "".join(delta.text for delta in _deltas(deltas, TextDelta)) == "hello"
    assert "".join(delta.text for delta in _deltas(deltas, ReasoningDelta)) == "think"
    tool_deltas = list(_deltas(deltas, ToolCallDelta))
    assert [delta.arguments for delta in tool_deltas] == ['{"q":', '"x"}']
    assert all(
        delta.call_id == "c1" and delta.name == "lookup" for delta in tool_deltas
    )
    usage = next(_deltas(deltas, UsageDelta)).usage
    assert usage.input_tokens == 9
    assert usage.output_tokens == 4
    assert usage.cache_read_tokens == 2
    entry = next(_deltas(deltas, EntryCompletedDelta)).entry
    assert isinstance(entry, ReasoningEntry)
    assert entry.content == "think"
    assert entry.provider == "openai-chat"
    assert next(_deltas(deltas, FinishDelta)).reason == "tool_calls"


@pytest.mark.asyncio
async def test_chat_http_errors_are_classified() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, content=b"rate limited")
        )
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert exc_info.value.vendor == "openai"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_chat_missing_official_api_key_raises_user_error(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIChatProvider(model="gpt-5", base_url="https://api.openai.com/v1")

    with pytest.raises(UserError, match="requires an API key"):
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))


@pytest.mark.asyncio
async def test_chat_transport_errors_are_classified() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert exc_info.value.vendor == "openai"
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_chat_context_overflow_is_classified() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400,
                content=b'{"error":{"code":"context_length_exceeded"}}',
            )
        )
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ContextOverflowError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    # Nothing to learn: the body states no limit.
    assert exc_info.value.reported_window is None


@pytest.mark.asyncio
async def test_chat_context_overflow_reports_the_stated_window() -> None:
    body = (
        b'{"error":{"code":"context_length_exceeded","message":"This model\'s '
        b'maximum context length is 65536 tokens. However, you requested 190402 '
        b'tokens (182402 in the messages, 8000 in the completion)."}}'
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, content=body))
    )
    provider = OpenAIChatProvider(model="deepseek-chat", api_key="sk-test", client=client)

    with pytest.raises(ContextOverflowError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert exc_info.value.reported_window == 65_536


@pytest.mark.asyncio
async def test_non_overflow_error_leaves_reported_window_unset() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, content=b'{"error":"invalid api key"}')
        )
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    assert not isinstance(exc_info.value, ContextOverflowError)
    assert getattr(exc_info.value, "reported_window", None) is None


def test_build_payload_none_valued_option_removes_adapter_default() -> None:
    provider = OpenAIChatProvider(
        model="gpt-5", api_key="sk-test", base_url="https://example.test/v1"
    )

    payload = provider._build_payload(
        [InputEntry(role="user", content="hi")],
        tools=None,
        response_format=None,
        settings=ModelSettings(
            provider_options={"openai-chat": {"stream_options": None}}
        ),
        stream=True,
    )

    # Endpoints that reject stream_options need a way to strip the default.
    assert "stream_options" not in payload


def test_context_window_resolves_date_pinned_snapshots() -> None:
    provider = OpenAIChatProvider(model="gpt-4.1", api_key="sk-test")

    assert provider.context_window("gpt-4.1") == 1_047_576
    assert provider.context_window("gpt-4.1-2025-04-14") == 1_047_576
    assert provider.context_window("o3-2025-04-16") is None
    # GPT-5 releases disagree on their window, so the table stays exact:
    # "gpt-5.5" must never resolve through a "gpt-5" prefix.
    assert provider.context_window("gpt-5") == 400_000
    assert provider.context_window("gpt-5.5") == 1_050_000
    assert provider.context_window("gpt-5.5-pro") == 1_050_000
    assert provider.context_window("gpt-5-mini") is None


def test_context_window_constructor_argument_overrides_the_table() -> None:
    """The deployment's window, for endpoints the table cannot know."""
    provider = OpenAIChatProvider(
        model="qwen2.5",
        api_key="sk-test",
        base_url="http://localhost:8000/v1",
        context_window=32_768,
    )
    assert provider.context_window("qwen2.5") == 32_768

    # A vLLM host serving a familiar alias at a smaller --max-model-len.
    capped = OpenAIChatProvider(model="gpt-4.1", api_key="x", context_window=8_192)
    assert capped.context_window("gpt-4.1") == 8_192


@pytest.mark.asyncio
async def test_chat_stream_handles_null_arguments_and_missing_index() -> None:
    body = _sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                # Gateway style: complete calls, a null index,
                                # a missing index, and a null arguments field.
                                {
                                    "id": "c1",
                                    "index": None,
                                    "function": {"name": "one", "arguments": None},
                                },
                                {
                                    "id": "c2",
                                    "function": {
                                        "name": "two",
                                        "arguments": '{"b":2}',
                                    },
                                },
                            ]
                        }
                    }
                ]
            }
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    tool_deltas = list(_deltas(deltas, ToolCallDelta))
    assert [(d.index, d.call_id, d.name, d.arguments) for d in tool_deltas] == [
        (0, "c1", "one", ""),
        (1, "c2", "two", '{"b":2}'),
    ]


@pytest.mark.asyncio
async def test_chat_stream_assembles_interleaved_parallel_tool_calls() -> None:
    body = _sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "one", "arguments": '{"a":'},
                                },
                                {
                                    "index": 1,
                                    "id": "c2",
                                    "function": {"name": "two", "arguments": '{"b":'},
                                },
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 1, "function": {"arguments": "2}"}},
                                {"index": 0, "function": {"arguments": "1}"}},
                            ]
                        }
                    }
                ]
            },
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    by_index: dict[int, list[str]] = {}
    for delta in _deltas(deltas, ToolCallDelta):
        by_index.setdefault(delta.index, []).append(delta.arguments)
        assert delta.call_id == ("c1" if delta.index == 0 else "c2")
        assert delta.name == ("one" if delta.index == 0 else "two")
    assert "".join(by_index[0]) == '{"a":1}'
    assert "".join(by_index[1]) == '{"b":2}'


@pytest.mark.asyncio
async def test_chat_stream_without_usage_or_finish_reports_defaults() -> None:
    """Compatible endpoints may omit usage and finish_reason entirely."""
    body = _sse([{"choices": [{"delta": {"content": "hi"}}]}])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(provider.stream([InputEntry(role="user", content="hi")]))

    usage = next(_deltas(deltas, UsageDelta)).usage
    assert (usage.input_tokens, usage.output_tokens) == (0, 0)
    assert next(_deltas(deltas, FinishDelta)).reason is None


@pytest.mark.asyncio
async def test_chat_stream_truncated_without_finish_or_done_is_retryable() -> None:
    """A stream cut mid-tool-call (no finish_reason, no [DONE]) raises retryably.

    The transport closes cleanly at a frame boundary, so nothing else fires;
    without this guard the half-formed ``write_file`` call would be assembled
    and later 400 the next request when echoed back in history. Retryable so
    the run's RetryPolicy re-streams the turn.
    """
    body = _sse_truncated(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {
                                        "name": "write_file",
                                        # cut off mid-arguments, as in the incident
                                        "arguments": '{"path": "report.md"',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    )
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    with pytest.raises(ProviderError) as exc_info:
        await _collect(provider.stream([InputEntry(role="user", content="hi")]))
    assert exc_info.value.retryable is True
    assert "truncated" in str(exc_info.value)


@pytest.mark.asyncio
async def test_chat_stream_with_done_but_no_finish_is_not_truncated() -> None:
    """[DONE] alone (finish_reason omitted) is a complete stream, not a cut one."""
    body = _sse([{"choices": [{"delta": {"content": "hi"}}]}])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    )
    provider = OpenAIChatProvider(model="gpt-5", api_key="sk-test", client=client)

    deltas = await _collect(provider.stream([InputEntry(role="user", content="hi")]))
    assert "".join(d.text for d in _deltas(deltas, TextDelta)) == "hi"
    assert next(_deltas(deltas, FinishDelta)).reason is None


def test_tool_call_arguments_pass_through_byte_for_byte() -> None:
    # The serializer trusts the transcript invariant (args are valid JSON,
    # normalized at detection time) and forwards them unchanged.
    msg = Message(
        role="assistant",
        tool_calls=[ToolCall(id="c1", name="lookup", arguments='{"q":"x"}')],
    )

    out = message_to_openai(msg)

    assert out["tool_calls"][0]["function"]["arguments"] == '{"q":"x"}'


def test_supports_json_schema_defaults_to_official_openai_only() -> None:
    assert OpenAIChatProvider(
        model="gpt-5", base_url="https://api.openai.com/v1"
    ).supports_json_schema
    assert not OpenAIChatProvider(
        model="gpt-5", base_url="https://example.test/v1"
    ).supports_json_schema
    assert OpenAIChatProvider(
        model="gpt-5",
        base_url="https://example.test/v1",
        supports_json_schema=True,
    ).supports_json_schema


def test_regional_official_host_follows_official_dialect() -> None:
    provider = OpenAIChatProvider(
        model="gpt-5", api_key="sk-test", base_url="https://eu.api.openai.com/v1"
    )

    payload = provider._build_payload(
        [InputEntry(role="user", content="hi")],
        tools=None,
        response_format=None,
        settings=ModelSettings(max_tokens=50),
        stream=True,
    )

    assert payload["max_completion_tokens"] == 50
    assert "max_tokens" not in payload
    assert provider.supports_json_schema
    assert not provider._should_replay_reasoning()


def test_official_api_flag_opts_gateway_into_official_dialect() -> None:
    provider = OpenAIChatProvider(
        model="gpt-5",
        base_url="https://gateway.example.test/openai",
        official_api=True,
    )

    payload = provider._build_payload(
        [InputEntry(role="user", content="hi")],
        tools=None,
        response_format=None,
        settings=ModelSettings(max_tokens=50),
        stream=True,
    )

    assert payload["max_completion_tokens"] == 50
    assert provider.supports_json_schema
    assert not provider._should_replay_reasoning()
    # Dialect does not imply auth: a keyless gateway must stay usable.
    provider._check_ready()


def test_official_api_flag_can_force_compatible_dialect(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIChatProvider(
        model="gpt-5",
        base_url="https://api.openai.com/v1",
        official_api=False,
    )

    payload = provider._build_payload(
        [InputEntry(role="user", content="hi")],
        tools=None,
        response_format=None,
        settings=ModelSettings(max_tokens=50),
        stream=True,
    )

    assert payload["max_tokens"] == 50
    assert not provider.supports_json_schema
    # The real host still requires a key regardless of the dialect claim.
    with pytest.raises(UserError, match="requires an API key"):
        provider._check_ready()


def test_reasoning_delta_event_emitted_by_runner() -> None:
    provider = ScriptedProvider([text("done.", reasoning="thinking...")])
    agent = Agent(name="a", model=provider)

    async def go() -> tuple[str, str]:
        handle = Runner.stream(agent, "hi")
        text_d: list[str] = []
        reasoning_d: list[str] = []
        async for ev in handle:
            if isinstance(ev, events.TextDelta):
                text_d.append(ev.delta)
            elif isinstance(ev, events.ReasoningDelta):
                reasoning_d.append(ev.delta)
        return "".join(text_d), "".join(reasoning_d)

    text_out, reasoning_out = asyncio.run(go())
    assert text_out == "done."
    assert reasoning_out == "thinking..."


def test_is_context_overflow_classic_openai_code() -> None:
    body = '{"error":{"code":"context_length_exceeded","message":"..."}}'
    assert _is_context_overflow(400, body)


def test_is_context_overflow_modal_style_input_tokens_400() -> None:
    body = (
        '{"error":{"message":"You passed 131073 input tokens and requested '
        "0 output tokens. However, the model's context length is only "
        "131072 tokens, resulting in a maximum input length of 131072 "
        "tokens. Please reduce the length of the input prompt. "
        '(parameter=input_tokens, value=131073)","type":"BadRequestError",'
        '"param":"input_tokens","code":400}}'
    )
    assert _is_context_overflow(400, body)


def test_is_context_overflow_anthropic_style_413() -> None:
    assert _is_context_overflow(413, "request too large for the model")


def test_is_context_overflow_ignores_other_errors() -> None:
    assert not _is_context_overflow(400, "invalid api key")
    assert not _is_context_overflow(500, "context_length_exceeded")


def test_is_context_overflow_string_too_long_only_with_context() -> None:
    assert _is_context_overflow(400, "string too long; max context exceeded")
    assert not _is_context_overflow(400, "string too long: tool argument")
