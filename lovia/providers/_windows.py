"""Resolving a model's context window from what the endpoint tells us.

A context window is a fact about an *(endpoint, model, deployment)* triple, not
about a model name: the same ``qwen2.5`` is 32K on one vLLM host and 4K on
another, depending on ``--max-model-len``. A static name→int table can never be
authoritative, so the adapters treat theirs as the lowest-trust layer and let
two better sources overrule it:

* :func:`window_from_error` — the number the endpoint *itself* named when it
  rejected an oversized prompt. This is the endpoint refusing, so it outranks
  everything, including a user-configured window.
* :func:`window_from_models_payload` — the window an endpoint advertises up
  front on ``GET /models`` (vLLM, SGLang, OpenRouter, Groq, Together).

Both return ``None`` rather than a guess: a wrong window is worse than an
unknown one, because the unknown case already has a working fallback (reactive
overflow handling) while a wrong one silently mis-sizes every prompt.
"""

from __future__ import annotations

import re
from typing import Any

# Plausible bounds for a real context window. The floor rejects a parse that
# grabbed a rate-limit quota or a stray small integer; the ceiling rejects one
# that grabbed a byte count. Both are far outside any shipping model.
_MIN_WINDOW = 1024
_MAX_WINDOW = 20_000_000

# Phrases that mark a body as a *rate limit*, not a context overflow. Groq
# returns HTTP 413 "Request too large ... on tokens per minute (TPM): Limit
# 12000, Requested 14137" — which ``_is_context_overflow`` already classifies
# as an overflow via its "request too large" needle. Learning 12000 from it
# would pin the window to a per-minute quota *permanently*: an under-claimed
# window never overflows, so it never gets a chance to be corrected.
_RATE_LIMIT_MARKERS = (
    "tokens per minute",
    "requests per",
    "per minute",
    "per day",
    "rate limit",
    "rate_limit",
    "tpm",
    "rpm",
)

# Anchor on the phrasing that introduces the *limit*, never on a bare
# ``(\d+) tokens``: every OpenAI-family body carries the requested count too,
# and Anthropic prints it *first* ("208310 tokens > 200000 maximum").
_LIMIT_PATTERNS = (
    # OpenAI, Azure, DeepSeek, vLLM, Groq(400), OpenRouter ("This endpoint's ...")
    re.compile(r"maximum context length is\s+(\d+)", re.I),
    # "the model's context length is only 131072 tokens, resulting in a
    # maximum input length of 131072 tokens" — modal-hosted and similar.
    re.compile(r"context length is only\s+(\d+)", re.I),
    re.compile(r"maximum input length of\s+(\d+)", re.I),
    # Anthropic: "prompt is too long: 208310 tokens > 200000 maximum"
    re.compile(r">\s*(\d+)\s*maximum", re.I),
    # Together / TGI: "must not exceed 4097" / "must be <= 4097"
    re.compile(r"must not exceed\s+(\d+)", re.I),
    re.compile(r"must be\s*<=\s*(\d+)", re.I),
)

# Anthropic's second form: "input length and max_tokens exceed context limit:
# 188240 + 21333 > 200000, decrease input length or max_tokens". The comma
# makes this pattern too loose to run unconditionally, so it is gated on the
# surrounding phrase.
_CONTEXT_LIMIT_GATE = "exceed context limit"
_CONTEXT_LIMIT_PATTERN = re.compile(r">\s*(\d+)\s*,", re.I)


def window_from_error(body: str) -> int | None:
    """Extract the context window an overflow error names, if it names one.

    ``body`` is the raw error payload of a response already classified as a
    context overflow. Returns ``None`` when no limit is stated (llama.cpp says
    only "the request exceeds the available context size"), when the body is
    really a rate limit, or when the number is implausible.
    """
    lowered = body.lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        return None

    patterns = list(_LIMIT_PATTERNS)
    if _CONTEXT_LIMIT_GATE in lowered:
        patterns.append(_CONTEXT_LIMIT_PATTERN)

    # Several anchors can fire on one body ("context length is only N ...
    # maximum input length of N"). They describe the same ceiling, so the
    # smallest match is the safe reading of "the limit".
    found = [int(m.group(1)) for p in patterns if (m := p.search(body))]
    windows = [w for w in found if _MIN_WINDOW <= w <= _MAX_WINDOW]
    return min(windows) if windows else None


# Fields that carry a context window on a ``GET /models`` entry, most specific
# first. The *served* window beats the model's theoretical maximum: vLLM's
# ``max_model_len`` already reflects ``--max-model-len``, and OpenRouter's
# ``top_provider.context_length`` reflects the provider it actually routes to,
# which can be lower than the model-level number.
_WINDOW_FIELDS = (
    "max_model_len",  # vLLM, SGLang
    "context_window",  # Groq
    "loaded_context_length",  # LM Studio (/api/v0 only; harmless to accept)
    "context_length",  # OpenRouter (model-level), Together
    "max_context_length",  # LM Studio (theoretical max)
)


def _coerce_window(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if _MIN_WINDOW <= value <= _MAX_WINDOW else None


def _window_from_entry(entry: dict[str, Any]) -> int | None:
    top = entry.get("top_provider")
    if isinstance(top, dict):
        window = _coerce_window(top.get("context_length"))
        if window is not None:
            return window
    for field in _WINDOW_FIELDS:
        window = _coerce_window(entry.get(field))
        if window is not None:
            return window
    return None


def window_from_models_payload(payload: Any, model: str) -> int | None:
    """Read ``model``'s context window out of a ``GET /models`` response.

    ``None`` when the endpoint publishes no window for it — the official
    OpenAI, Anthropic and DeepSeek APIs publish nothing at all, and Ollama's
    and llama.cpp's OpenAI-compatible listings carry no window either.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    # A vendor prefix is a lovia routing concept; the endpoint knows the bare id.
    wanted = model.split(":", 1)[1] if ":" in model else model
    for entry in data:
        if isinstance(entry, dict) and entry.get("id") == wanted:
            return _window_from_entry(entry)
    return None


# ---------------------------------------------------------------------------
# The bundled table: a hint, not an authority
# ---------------------------------------------------------------------------

# Date-pinned snapshots share their alias's window. OpenAI writes
# "gpt-4.1-2025-04-14", Anthropic writes "claude-sonnet-4-5-20250929".
_SNAPSHOT_SUFFIX = re.compile(r"-(?:\d{4}-\d{2}-\d{2}|\d{8})$")


def strip_snapshot(model: str) -> str:
    """Drop a trailing date-pinned snapshot suffix from a model name."""
    return _SNAPSHOT_SUFFIX.sub("", model)


def table_window(model: str, rules: tuple[tuple[str, int], ...]) -> int | None:
    """Look ``model`` up in ``rules``: exact match, then longest prefix.

    An exact rule is a fact about one alias; a prefix rule is a fact about a
    naming family, and the longest one wins so ``gpt-5.5-pro`` never resolves
    through ``gpt-5``. Unlisted models return ``None`` rather than a guess —
    the resolution chain has better sources than this table.
    """
    name = strip_snapshot(model)
    for key, window in rules:
        if name == key:
            return window
    best: tuple[int, int] | None = None  # (len(prefix), window)
    for key, window in rules:
        if key.endswith("-") and name.startswith(key):
            if best is None or len(key) > best[0]:
                best = (len(key), window)
    return best[1] if best is not None else None
