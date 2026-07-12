"""Resolving a model's context window from what the endpoint tells us.

A context window is a fact about an *(endpoint, model, deployment)* triple, not
about a model name: the same ``qwen2.5`` is 32K on one vLLM host and 4K on
another, depending on ``--max-model-len``. So the bundled tables are keyed by
*host* — ``gpt-4.1`` means 1M on ``api.openai.com`` and nothing at all on a box
that merely re-exposes the name — and even then they are the lowest-trust layer,
which two better sources overrule:

* :func:`window_from_error` — the number the endpoint *itself* named when it
  rejected an oversized prompt. This is the endpoint refusing, so it outranks
  everything; adapters ship it on ``ContextOverflowError.reported_window`` and
  the context policy learns and persists it per session.
* :func:`window_from_models_payload` — the window an endpoint advertises up
  front on ``GET /models`` (vLLM, SGLang, OpenRouter, Groq, Together).

Both return ``None`` rather than a guess: a wrong window is worse than an
unknown one, because the unknown case already has a working fallback (reactive
overflow handling) while a wrong one silently mis-sizes every prompt.

:class:`WindowResolver` combines the advertised listing with the table and
memoizes the probe per process — a ``"vendor:model"`` string is resolved into
a fresh provider on every run and every handoff. Budgets live elsewhere: the
one user-facing window knob is ``Compaction(context_window=...)``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeGuard

import httpx

from ._http import host_matches

# A bundled table: endpoint host → its models' windows. Values are exact model
# aliases or, when a whole naming family agrees, a prefix ending in ``-``.
WindowTable = Mapping[str, tuple[tuple[str, int], ...]]

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
    "max_input_tokens",  # Anthropic Models API (since 2026-03), LiteLLM proxies
    "loaded_context_length",  # LM Studio (/api/v0 only; harmless to accept)
    "context_length",  # OpenRouter (model-level), Together
    "max_context_length",  # LM Studio (theoretical max)
)


def plausible_window(value: Any) -> TypeGuard[int]:
    """Whether ``value`` could be a real context window.

    The bounds are the same wherever a window arrives from outside the process
    — a parsed error, a ``/models`` listing, a hand-edited scratch file. A wrong
    window is worse than an unknown one, and a *small* wrong one is worst of
    all: it never overflows, so nothing ever corrects it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return _MIN_WINDOW <= value <= _MAX_WINDOW


def _coerce_window(value: Any) -> int | None:
    return value if plausible_window(value) else None


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
    OpenAI and DeepSeek APIs publish nothing at all, and Ollama's and
    llama.cpp's OpenAI-compatible listings carry no window either. The
    official Anthropic API *does*: ``max_input_tokens``, since 2026-03.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    # The literal id first: a colon is lovia's vendor separator, but it is also
    # part of Ollama's own names (``llama3:8b``). Only if nothing matches do we
    # retry with the vendor prefix stripped.
    candidates = [model]
    if ":" in model:
        candidates.append(model.split(":", 1)[1])
    for wanted in candidates:
        for entry in data:
            if isinstance(entry, dict) and entry.get("id") == wanted:
                return _window_from_entry(entry)
    return None


# The probe runs before the first model call, so its latency is charged to run
# start. The provider timeout (300s by default) is sized for generation, not for
# a metadata lookup that is pure upside: a slow endpoint should cost a moment
# and then be forgotten, never stall the run.
_PROBE_TIMEOUT = 10.0


async def fetch_reported_window(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    model: str,
    params: Mapping[str, str | int] | None = None,
) -> int | None:
    """Ask ``GET {base_url}/models`` what ``model``'s context window is.

    Fails open: an unreachable endpoint, a slow one, an error status, a non-JSON
    body or a listing without window metadata all yield ``None``. The caller is
    trying to do better than "unknown", so nothing here is worth raising over.

    ``params`` lets an adapter shape the listing for its dialect — Anthropic's
    paginates at 20 entries by default, so its adapter asks for a page large
    enough to hold the whole catalog.
    """
    try:
        response = await client.get(
            f"{base_url}/models",
            headers=headers,
            params=params,
            follow_redirects=True,
            timeout=_PROBE_TIMEOUT,
        )
        if not response.is_success:
            return None
        return window_from_models_payload(response.json(), model)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError):
        # InvalidURL is not an HTTPError: a malformed base_url must degrade to
        # "unknown" here and fail loudly on the model call itself.
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


def rules_for_host(host: str, table: WindowTable) -> tuple[tuple[str, int], ...]:
    """The window rules that apply to ``host``, or none at all.

    The table is keyed by *host* because ``gpt-4.1`` on ``api.openai.com`` is a
    fact about OpenAI's deployment, and says nothing about the ``gpt-4.1`` a
    vLLM box re-exposes at ``--max-model-len 8192``. An unknown host gets no
    rules and falls through to what the endpoint reports about itself.
    """
    for domain, rules in table.items():
        if host_matches(host, (domain,)):
            return rules
    return ()


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


# ---------------------------------------------------------------------------
# Remembering what an endpoint told us
# ---------------------------------------------------------------------------

# A window belongs to the endpoint, not to the provider object that happened to
# ask. A string model spec ("deepseek-v4-pro") is resolved into a *fresh*
# provider on every run and on every handoff, so a memo living on the instance
# would re-probe ``/models`` forever. ``_ADVERTISED`` is what a listing claims;
# it outranks the bundled table. The window an endpoint names while *refusing*
# a prompt is not recorded here at all — the context policy learns it from
# ``ContextOverflowError.reported_window`` and persists it per session.
_ADVERTISED: dict[tuple[str, str], int] = {}
_PROBED: set[tuple[str, str]] = set()


def clear_endpoint_cache() -> None:
    """Forget every remembered endpoint window. For tests."""
    _ADVERTISED.clear()
    _PROBED.clear()


class WindowResolver:
    """One ``(endpoint, model)``'s context window, and the memo behind it.

    Owns what the *endpoint* says about itself: what it advertised on
    ``/models`` (probed at most once per process), then the bundled table.
    Budgets are not its business — the one user-facing window knob is
    ``Compaction(context_window=...)``, and the limit an endpoint names while
    rejecting a prompt travels via ``ContextOverflowError.reported_window``
    into the context policy's per-session state.
    """

    def __init__(
        self,
        *,
        base_url: str,
        host: str,
        model: str,
        table: WindowTable,
        probe: bool,
    ) -> None:
        self._key = (base_url, model)
        self._rules = rules_for_host(host, table)
        self._probe = probe

    def window(self) -> int | None:
        """This endpoint's window for this model, without any I/O."""
        advertised = _ADVERTISED.get(self._key)
        if advertised is not None:
            return advertised
        _, model = self._key
        return table_window(model, self._rules)

    async def discover(self, fetch: Callable[[], Awaitable[int | None]]) -> int | None:
        """Ask what the endpoint publishes — at most once per process.

        A no-op when this endpoint is known to publish none or when it was
        already asked. Returns whatever :meth:`window` would, so callers never
        special-case a miss.
        """
        if self._probe and self._key not in _PROBED:
            window = await fetch()
            _PROBED.add(self._key)
            if window is not None:
                _ADVERTISED.setdefault(self._key, window)
        return self.window()
