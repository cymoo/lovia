"""Extracting a context window from what an endpoint says.

Every body below is a real one, quoted verbatim: the whole point of the
anchored patterns is that they survive the wording each vendor actually ships.
"""

from __future__ import annotations

import pytest

from lovia.providers._windows import (
    strip_snapshot,
    table_window,
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
    'in a maximum input length of 131072 tokens. Please reduce the length of the '
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
    assert window_from_error(f"This model's maximum context length is {value} tokens") is None


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


def test_models_payload_reads_together_context_length() -> None:
    payload = _payload({"id": "mistral-7b", "context_length": 8192})
    assert window_from_models_payload(payload, "mistral-7b") == 8192


def test_models_payload_prefers_openrouter_top_provider() -> None:
    """``top_provider`` reflects the provider actually routed to; it can be lower."""
    payload = _payload(
        {
            "id": "anthropic/claude-sonnet-4-5",
            "context_length": 1_000_000,
            "top_provider": {"context_length": 200_000, "max_completion_tokens": 64_000},
        }
    )
    assert window_from_models_payload(payload, "anthropic/claude-sonnet-4-5") == 200_000


def test_models_payload_strips_the_vendor_prefix() -> None:
    payload = _payload({"id": "gpt-4.1", "max_model_len": 4096})
    assert window_from_models_payload(payload, "openai:gpt-4.1") == 4096


@pytest.mark.parametrize(
    "payload",
    [
        # The official OpenAI/Anthropic/DeepSeek shape: no window anywhere.
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
# The bundled table: exact match, then longest prefix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-4.1-2025-04-14", "gpt-4.1"),  # OpenAI's YYYY-MM-DD
        ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5"),  # Anthropic's YYYYMMDD
        ("gpt-4.1", "gpt-4.1"),
        ("deepseek-v4-pro", "deepseek-v4-pro"),
        ("model-12345", "model-12345"),  # not a date
    ],
)
def test_strip_snapshot(model: str, expected: str) -> None:
    assert strip_snapshot(model) == expected


_RULES = (
    ("gpt-5", 400_000),
    ("gpt-5.5", 1_050_000),
    ("claude-sonnet-", 200_000),
    ("claude-sonnet-4-5", 111_111),  # an exact rule beats its own family
)


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5", 400_000),
        ("gpt-5.5", 1_050_000),  # exact, not via a "gpt-5" prefix
        ("gpt-5-mini", None),  # exact rules never match by prefix
        ("claude-sonnet-4-6", 200_000),  # family prefix
        ("claude-sonnet-4-6-20260101", 200_000),  # snapshot, then family
        ("claude-sonnet-4-5", 111_111),  # exact wins over the family
        ("claude-opus-4-8", None),
        ("", None),
    ],
)
def test_table_window(model: str, expected: int | None) -> None:
    assert table_window(model, _RULES) == expected


def test_table_window_prefers_the_longest_prefix() -> None:
    rules = (("a-", 1024), ("a-b-", 2048), ("a-b-c-", 4096))
    assert table_window("a-x", rules) == 1024
    assert table_window("a-b-x", rules) == 2048
    assert table_window("a-b-c-x", rules) == 4096
