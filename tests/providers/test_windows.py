"""Extracting a context window from what an endpoint says.

Every body below is a real one, quoted verbatim: the whole point of the
anchored patterns is that they survive the wording each vendor actually ships.
"""

from __future__ import annotations

import asyncio

import pytest

from lovia.providers import _windows
from lovia.providers._windows import (
    ProbeUnavailable,
    WindowResolver,
    window_from_error,
    window_from_models_payload,
)


# ---------------------------------------------------------------------------
# window_from_error — the limit, never the requested count
# ---------------------------------------------------------------------------

OPENAI = (
    "This model's maximum context length is 4097 tokens. However, you requested "
    "4097 tokens (1647 in the messages, 2450 in the completion). Please reduce "
    "the length of the messages or completion."
)
OPENAI_RESULTED_IN = (
    "This model's maximum context length is 8192 tokens. However, your messages "
    "resulted in 44366 tokens. Please reduce the length of the messages."
)
AZURE = (
    "(context_length_exceeded) This model's maximum context length is 128000 "
    "tokens for gpt-4.1"
)
DEEPSEEK = (
    "This model's maximum context length is 65536 tokens. However, you requested "
    "190402 tokens (182402 in the messages, 8000 in the completion). Please "
    "reduce the length of the messages or completion."
)
VLLM = (
    "This model's maximum context length is 16384 tokens. However, you requested "
    "122946 tokens (112946 in the messages, 10000 in the completion)."
)
OPENROUTER = (
    "This endpoint's maximum context length is 200000 tokens. However, you "
    "requested about 5028244 tokens (4945291 of text input, 2953 of tool input, "
    "80000 in the output). Please reduce the length of either one, or use the "
    '"middle-out" transform to compress your prompt automatically.'
)
# Requested count comes *first* in both Anthropic forms.
ANTHROPIC_PROMPT_TOO_LONG = "prompt is too long: 208310 tokens > 200000 maximum"
ANTHROPIC_CONTEXT_LIMIT = (
    "input length and max_tokens exceed context limit: 188240 + 21333 > 200000, "
    "decrease input length or max_tokens and try again"
)
# Requested first, and the limit is phrased "is only".
MODAL = (
    '{"error":{"message":"You passed 131073 input tokens and requested 0 output '
    "tokens. However, the model's context length is only 131072 tokens, resulting "
    "in a maximum input length of 131072 tokens. Please reduce the length of the "
    'input prompt."}}'
)
TOGETHER = (
    "Input validation error: The sum of 'inputs' tokens and 'max_new_tokens' "
    "must not exceed 4097. Please adjust your inputs accordingly."
)
TOGETHER_LTE = "`inputs` tokens + `max_new_tokens` must be <= 4097"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (OPENAI, 4097),  # requested == limit
        (OPENAI_RESULTED_IN, 8192),
        (AZURE, 128_000),
        (DEEPSEEK, 65_536),
        (VLLM, 16_384),
        (OPENROUTER, 200_000),  # "This endpoint's", not "This model's"
        (ANTHROPIC_PROMPT_TOO_LONG, 200_000),  # not 208310
        (ANTHROPIC_CONTEXT_LIMIT, 200_000),  # not 188240 or 21333
        (MODAL, 131_072),  # not 131073
        (TOGETHER, 4097),
        (TOGETHER_LTE, 4097),
    ],
)
def test_window_from_error_extracts_the_limit(body: str, expected: int) -> None:
    assert window_from_error(body) == expected


# Groq answers HTTP 413 with a *per-minute quota*, and its wording trips the
# adapter's "request too large" overflow needle. Learning 12000 from it would
# pin the window to a quota forever: an under-claimed window never overflows,
# so nothing would ever correct it.
GROQ_TPM = (
    "Request too large for model `llama-3.3-70b-versatile` in organization "
    "`org_abc` service tier `on_demand` on tokens per minute (TPM): Limit 12000, "
    "Requested 14137, please reduce your message size and try again."
)


@pytest.mark.parametrize(
    "body",
    [
        GROQ_TPM,
        "Rate limit reached: Limit 30000, Requested 40000",
        "You have exceeded your requests per day quota of 200000",
    ],
)
def test_window_from_error_refuses_rate_limits(body: str) -> None:
    assert window_from_error(body) is None


@pytest.mark.parametrize(
    "body",
    [
        # llama.cpp states no number at all.
        "the request exceeds the available context size, try increasing it",
        "context_length_exceeded",
        "prompt is too long",
        "invalid api key",
        "",
    ],
)
def test_window_from_error_without_a_stated_limit(body: str) -> None:
    assert window_from_error(body) is None


def test_window_from_error_gates_the_comma_pattern() -> None:
    """``> N,`` is too loose to run unless the body says "exceed context limit"."""
    assert window_from_error("shard count 4 > 200000, retry") is None
    assert window_from_error(ANTHROPIC_CONTEXT_LIMIT) == 200_000


@pytest.mark.parametrize("value", [0, 1, 1023, 20_000_001, 10**12])
def test_window_from_error_rejects_implausible_numbers(value: int) -> None:
    assert (
        window_from_error(f"This model's maximum context length is {value} tokens")
        is None
    )


def test_window_from_error_takes_the_smallest_anchor() -> None:
    """Anchors that fire on one body describe one ceiling; the min is safe."""
    body = (
        "the model's context length is only 32768 tokens, resulting in a "
        "maximum input length of 30000 tokens"
    )
    assert window_from_error(body) == 30_000


# ---------------------------------------------------------------------------
# window_from_models_payload — what an endpoint advertises up front
# ---------------------------------------------------------------------------


def _payload(*entries: dict) -> dict:
    return {"object": "list", "data": list(entries)}


def test_models_payload_reads_vllm_max_model_len() -> None:
    payload = _payload({"id": "qwen2.5", "object": "model", "max_model_len": 32_768})
    assert window_from_models_payload(payload, "qwen2.5") == 32_768


def test_models_payload_reads_groq_context_window() -> None:
    payload = _payload({"id": "llama-3.3-70b", "context_window": 131_072})
    assert window_from_models_payload(payload, "llama-3.3-70b") == 131_072


def test_models_payload_reads_anthropic_max_input_tokens() -> None:
    """The official Anthropic Models API shape, published since 2026-03."""
    payload = _payload(
        {
            "type": "model",
            "id": "claude-opus-4-8",
            "display_name": "Claude Opus 4.8",
            "max_input_tokens": 1_000_000,
            "max_tokens": 128_000,  # the output cap — must not be read
        }
    )
    assert window_from_models_payload(payload, "claude-opus-4-8") == 1_000_000


def test_models_payload_reads_together_context_length() -> None:
    payload = _payload({"id": "mistral-7b", "context_length": 8192})
    assert window_from_models_payload(payload, "mistral-7b") == 8192


def test_models_payload_prefers_openrouter_top_provider() -> None:
    """``top_provider`` reflects the provider actually routed to; it can be lower."""
    payload = _payload(
        {
            "id": "anthropic/claude-sonnet-4-5",
            "context_length": 1_000_000,
            "top_provider": {
                "context_length": 200_000,
                "max_completion_tokens": 64_000,
            },
        }
    )
    assert window_from_models_payload(payload, "anthropic/claude-sonnet-4-5") == 200_000


def test_models_payload_strips_the_vendor_prefix() -> None:
    payload = _payload({"id": "gpt-4.1", "max_model_len": 4096})
    assert window_from_models_payload(payload, "openai:gpt-4.1") == 4096


@pytest.mark.parametrize(
    "payload",
    [
        # The official OpenAI/DeepSeek shape: no window anywhere.
        _payload({"id": "gpt-5.5", "object": "model", "owned_by": "openai"}),
        _payload({"id": "other-model", "max_model_len": 4096}),  # id mismatch
        _payload({"id": "m", "max_model_len": "lots"}),  # wrong type
        _payload({"id": "m", "max_model_len": 12}),  # implausible
        _payload({"id": "m", "max_model_len": True}),  # bool is not an int here
        {"data": "not-a-list"},
        {"object": "list"},
        [],
        None,
        "",
    ],
)
def test_models_payload_returns_none_when_unknown(payload: object) -> None:
    assert window_from_models_payload(payload, "m") is None
    assert window_from_models_payload(payload, "gpt-5.5") is None


# ---------------------------------------------------------------------------
# WindowResolver — the per-endpoint memo
# ---------------------------------------------------------------------------


def _resolver(**kw) -> WindowResolver:
    defaults = dict(base_url="http://gw/v1", model="m", probe=True)
    return WindowResolver(**{**defaults, **kw})


async def test_resolver_knows_nothing_until_the_endpoint_answers() -> None:
    """No bundled guess: a name alone never yields a window."""

    async def fetch() -> int | None:
        return 8_192

    assert _resolver().window() is None
    assert await _resolver().discover(fetch) == 8_192


async def test_the_advertised_memo_outlives_the_provider_that_probed() -> None:
    """A string model spec builds a fresh provider every run and every handoff.

    Without a per-endpoint memo every run would re-probe ``/models``.
    """

    async def fetch() -> int | None:
        return 8_192

    await _resolver().discover(fetch)
    assert _resolver().window() == 8_192  # a fresh resolver, no I/O
    # ...but only for that endpoint.
    assert _resolver(base_url="http://other/v1").window() is None


async def test_discover_asks_once_and_caches_the_miss() -> None:
    calls = 0

    async def fetch() -> int | None:
        nonlocal calls
        calls += 1
        return None

    assert await _resolver().discover(fetch) is None
    assert await _resolver().discover(fetch) is None  # a *fresh* resolver
    assert calls == 1


async def test_a_passing_failure_is_asked_again_after_a_cooldown(monkeypatch) -> None:
    """A 5xx or a timeout says nothing about the window: remembering it as a
    miss would cost every later session of the process its proactive budget."""
    now = [1_000.0]
    monkeypatch.setattr(_windows.time, "monotonic", lambda: now[0])
    answers: list[int | None | Exception] = [ProbeUnavailable("HTTP 503"), 8_192]

    async def fetch() -> int | None:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert await _resolver().discover(fetch) is None
    assert await _resolver().discover(fetch) is None  # within the cooldown
    assert len(answers) == 1
    now[0] += _windows._RETRY_AFTER
    assert await _resolver().discover(fetch) == 8_192


async def test_discover_declines_to_spend_a_request() -> None:
    """``probe=False`` marks an endpoint known to publish nothing."""

    async def fetch() -> int | None:  # pragma: no cover - must never run
        raise AssertionError("probed an endpoint that could not help")

    assert await _resolver(probe=False).discover(fetch) is None


async def test_concurrent_probes_all_see_the_window() -> None:
    async def slow_hit() -> int | None:
        await asyncio.sleep(0.01)
        return 32_768

    results = await asyncio.gather(
        _resolver().discover(slow_hit), _resolver().discover(slow_hit)
    )
    assert results == [32_768, 32_768]  # never a premature None


def test_models_payload_matches_an_id_that_contains_a_colon() -> None:
    """A colon is lovia's vendor separator, but Ollama puts one in its names."""
    payload = _payload({"id": "llama3:8b", "max_model_len": 8192})
    assert window_from_models_payload(payload, "llama3:8b") == 8192
