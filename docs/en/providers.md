# Providers & models

lovia is provider-neutral without an adapter tax: two built-in providers
speak OpenAI Chat Completions and Anthropic Messages directly over `httpx`,
any OpenAI-compatible endpoint rides the first one, and a custom vendor is a
`Protocol` implementation — not a subclassing project.

```python
from lovia import Agent, ModelSettings

agent = Agent(
    name="assistant",
    model="anthropic:<model>",
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

## Model strings

`Agent(model=...)` accepts a `"vendor:model"` string or a `Provider`
instance.

| Prefix | Provider | Aliases |
| --- | --- | --- |
| `openai:` | OpenAI Chat Completions | `oai:`, `openai-chat:` |
| `anthropic:` | Anthropic Messages | `claude:` |
| *(none)* | OpenAI Chat Completions | — |

A **bare name** (`"endpoint-model"`) routes to the OpenAI-compatible
provider — the intended spelling for `OPENAI_BASE_URL` services. One guard:
a bare name starting with `claude` logs a warning, since it is almost always
a missing `anthropic:` prefix. There is deliberately no default model:
running an agent without one raises `UserError`.

To avoid hard-coding models in scripts, `model_from_env()` reads `LOVIA_MODEL`
— the single knob — raising with a setup hint when it is not set
(`required=False` returns `None` instead).

## The OpenAI provider

`OpenAIChatProvider(model, *, api_key=None, base_url=None, client=None,
timeout=None, default_headers=None, supports_json_schema=None,
trust_env=None, replay_reasoning=None, official_dialect=None)`

Credentials and endpoint resolve from the environment when not passed:
`OPENAI_API_KEY`, `OPENAI_BASE_URL` (default
`https://api.openai.com/v1`).

**OpenAI-compatible endpoints** (DeepSeek, Ollama, vLLM, LM Studio, ...):
point `OPENAI_BASE_URL` at the service and use bare model names. The
adapter adjusts dialect by host: the official API gets
`max_completion_tokens` and native `response_format` JSON schema;
compatible endpoints get legacy `max_tokens`, and structured output falls
back to the [prompt path](structured-output.md#how-the-schema-reaches-the-model)
unless you pass `supports_json_schema=True`. A missing API key is an error
only on the official host — keyless local endpoints just work. When host
inference guesses wrong (a proxy in front of the official API, say),
`official_dialect=` overrides it; auth stays with the real host, so a
keyless gateway keeps working.

**Reasoning models** (DeepSeek-style `reasoning_content`): thinking streams
as [`ReasoningDelta`](streaming.md#model-output) events and is stored as
reasoning entries. On the next request, some hosts *require* those entries
echoed back (DeepSeek's thinking models return 400 otherwise) while the
official API rejects the field — so replay defaults per host:
`api.deepseek.com` → on, official API → off, other compatible endpoints →
on. `replay_reasoning=` forces either way. Only entries produced by this
provider are replayed.

## The Anthropic provider

`AnthropicProvider(model, *, api_key=None, base_url=None, client=None,
timeout=None, anthropic_version="2023-06-01", default_max_tokens=16_384,
default_headers=None, trust_env=None, official_dialect=None)`

Credentials and endpoint resolve from the environment when not passed:
`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` (default
`https://api.anthropic.com/v1`). The Messages API requires `max_tokens` on
every request, so when `settings.max_tokens` is unset the adapter sends
`default_max_tokens` (16,384 — matching the default context policy's output
reservation).

**Extended thinking**: enable it per Anthropic's API via provider options —

```python
settings = ModelSettings(
    max_tokens=16_000,
    provider_options={
        "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 8_000}}
    },
)
```

Thinking streams as `ReasoningDelta`; signatures and `redacted_thinking`
blocks round-trip intact. Replay is host-aware: the official API rejects
thinking blocks on requests that don't enable thinking, so stale blocks are
stripped there — while think-by-default compatible hosts (e.g. DeepSeek's
`/anthropic` flavor) always get them echoed back.

**Anthropic-flavored endpoints**: DeepSeek and others expose Anthropic
Messages dialects; point `ANTHROPIC_BASE_URL` at them and the leniencies
above apply automatically.

## Multi-vendor failover

One agent speaks to one provider; lovia deliberately has no in-process
fallback chain. Transient errors are the
[retry policy](retries.md)'s job. For vendor-level
failover, point `base_url` at a routing gateway (LiteLLM, OpenRouter, ...)
that fails over server-side — the run keeps a single endpoint, a single
context window, and a single capability set. And because sessions persist
across runs, an app can always re-run a failed request against the same
session with a different model.

## ModelSettings

Sampling parameters forwarded to the provider; `None` means "don't send",
so provider defaults apply.

| Field | Sent as |
| --- | --- |
| `temperature` | as-is |
| `top_p` | as-is |
| `max_tokens` | `max_completion_tokens` on official OpenAI; `max_tokens` elsewhere |
| `stop` | `stop` (OpenAI) / `stop_sequences` (Anthropic) |
| `parallel_tool_calls` | as-is (OpenAI); `disable_parallel_tool_use` tool-choice (Anthropic) — the *request-side* twin of [`Tool.parallel`](tools.md#parallel-execution-and-barriers) |
| `provider_options` | vendor-keyed extras, below |

**`provider_options`** is the escape hatch for vendor-specific parameters
without framework releases: a dict keyed by vendor whose entries merge into
the request payload verbatim.

```python
ModelSettings(provider_options={
    "openai": {"logprobs": True},
    "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 4_000}},
})
```

Adapters read their own key(s) — `"openai"` then `"openai-chat"`, or
`"anthropic"` then `"claude"`, later keys overriding — and a `None` value
*removes* a field the adapter would have sent (e.g.
`{"stream_options": None}`).

## Prompt caching

Provider caches make long agent loops affordable — the system prompt and
tool schemas are re-sent every turn, and lovia's
[compaction keeps the prompt prefix byte-stable](context.md) precisely so
they stay cached.

- **OpenAI**: caching is automatic server-side; the adapter surfaces
  `prompt_tokens_details.cached_tokens` as `usage.cache_read_tokens`.
- **Anthropic**: caching is explicit. Opt in per agent and the adapter
  places `cache_control: {"type": "ephemeral"}` breakpoints on the **last
  system block and the last tool definition** (the stable prefix):

  ```python
  settings = ModelSettings(provider_options={"anthropic": {"cache_system": True}})
  ```

  Cache reads/writes surface as `usage.cache_read_tokens` /
  `usage.cache_write_tokens`.

Either way, `usage.input_tokens` is the **full** prompt size, cached tokens
included — the cache fields break the total down; they don't add to it.
Keep the prefix stable to benefit: volatile
[dynamic instructions](agents.md#instructions) (timestamps, request ids)
bust the cache every turn.

## Context windows

A context window is a fact about an *(endpoint, model, deployment)* triple, not
about a model name: the same `qwen2.5` is 32K on one vLLM host and 4K on
another. So the default [`Compaction`](context.md) policy resolves it through a
chain, most trustworthy first:

| Source | When it answers |
| --- | --- |
| Explicit config | `Compaction(context_window=…)` — also settable via `--context-window` / `LOVIA_CONTEXT_WINDOW` in `lovia web`. The **one** user-facing knob; everything below is automatic |
| **The endpoint's own rejection** | after one overflow: providers name the limit ("maximum context length is 65536 tokens") |
| **The endpoint's `/models` listing** | vLLM and SGLang publish `max_model_len`; the official Anthropic API publishes `max_input_tokens`; Groq, Together and OpenRouter publish theirs |
| The bundled table | keyed by **host**: OpenAI's aliases on `api.openai.com`, the whole Claude line on `api.anthropic.com`, DeepSeek's on `api.deepseek.com` |
| — | otherwise unknown: no proactive compaction, reactive overflow handling only |

Only the second source may *override* an explicit setting, and only downward —
it is the endpoint enforcing a limit, not a guess. Configure `100_000` on a 200K
model and you keep 100K.

The table is keyed by host because `gpt-4.1` is a fact about *OpenAI's*
deployment. The `gpt-4.1` a vLLM box re-exposes at `--max-model-len 8192` is a
different thing entirely, and this adapter serves both — so an unlisted host
gets nothing from the table and relies on what the endpoint reports.

The practical upshot: an unlisted model costs **one** overflow per session. The
window the endpoint named travels on the error into the policy's state and is
persisted with the session, so every later turn — and every later run on that
session — sizes itself correctly.

Two consequences worth knowing:

- **The `/models` probe is skipped whenever it cannot help**: when the policy
  already has a configured window, when the endpoint is known to publish none
  (`api.openai.com`), and when it was already asked. Answers — misses included
  — are memoized per `(endpoint, model)` for the life of the process, so an
  endpoint is asked at most once even though a `"vendor:model"` string is
  resolved into a fresh provider on every run and every handoff. The probe runs
  before the first model call with its own short timeout; a slow endpoint costs
  a moment, never the run.
- **Anthropic windows come from the endpoint.** The official Models API
  publishes `max_input_tokens` per model, so the probe reads the window as
  served to *your* org — 1M on the current generation, 200K on older models.
  The bundled table only bridges the gap when the probe cannot answer.

## Custom providers

A provider is a `Protocol` — four members, no base class:

```python
class Provider(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def model(self) -> str | None: ...
    @property
    def supports_json_schema(self) -> bool: ...
    def stream(
        self, entries, *, tools=None, response_format=None, settings=None
    ) -> AsyncIterator[ModelDelta]: ...
```

`stream` receives the transcript view as `TranscriptEntry` values (richer
than chat messages — reasoning and metadata intact) and yields `ModelDelta`
values: `TextDelta`, `ReasoningDelta`, `ToolCallDelta`, `UsageDelta`,
`FinishDelta`, `EntryCompletedDelta`. Three optional protocols make a custom
provider a first-class citizen of compaction:

```python
class ContextWindowProvider(Protocol):     # report the window locally, no I/O
    def context_window(self) -> int | None: ...

class ContextWindowDiscovery(Protocol):    # one async lookup against the endpoint
    async def discover_context_window(self) -> int | None: ...

class TokenEstimator(Protocol):            # better-than-heuristic counting
    def estimate_tokens(self, entries) -> int: ...
```

These are `runtime_checkable`, which only checks that the method *exists* — a
wrong signature fails at call time. 
[`ScriptedProvider`](testing.md) in `lovia.testing` is a complete, readable
reference implementation.

Register a vendor prefix for string specs:

```python
from lovia.providers import register_provider

register_provider("mistral", lambda model: MistralProvider(model=model))
agent = Agent(name="x", model="mistral:large-3")
```

Or ship it in a package via the `lovia.providers` entry-point group — the
prefix resolves lazily on first use. Entry points cannot shadow the built-in
`openai:` / `anthropic:` prefixes (installing a package must never silently
reroute them); an explicit `register_provider` call can.

## Networking: timeouts, proxies, TLS

Both adapters share one HTTP configuration layer:

| Env var | Effect | Default |
| --- | --- | --- |
| `LOVIA_PROVIDER_TIMEOUT` | request timeout, seconds | `300` |
| `LOVIA_PROVIDER_TRUST_ENV` | honor `HTTP(S)_PROXY` / `NO_PROXY` | off |
| `LOVIA_HTTP_CA_BUNDLE` | custom PEM bundle for outbound TLS | — |
| `LOVIA_HTTP_INSECURE` | disable certificate verification | off |

Constructor arguments (`timeout=`, `trust_env=`) win over the environment.
TLS verification resolves in order: `LOVIA_HTTP_INSECURE` → the CA bundle →
the OS trust store when the optional `truststore` package is installed
(bundled with `lovia[web]`) → `certifi`. The same resolution covers the
[fetching tools](built-in-tools.md#reading-web-pages), so one intranet-CA
setting fixes every outbound request.

**Error classification** feeds the [retry machinery](retries.md):
HTTP 408/429/5xx and transport-level timeouts/disconnects are retryable
`ProviderError`s; context-length failures are detected per vendor
(status + message needles) and raised as `ContextOverflowError`, which
triggers reactive compaction instead of retries. That error also carries
`reported_window` — the limit the endpoint named, when it named one.

## Sharp edges

- **Ollama needs an explicit `context_window`.** It does not report an
  overflow at all: it silently truncates the prompt to `num_ctx` (default
  4096), dropping the *oldest* tokens first — your system prompt and tool
  definitions. Its OpenAI-compatible `/models` publishes no window either,
  so nothing in the resolution chain can reach it, and no error will ever
  tell you. Set `Compaction(context_window=…)` to match your `num_ctx`.
- **Anthropic prompt caching is opt-in** (`cache_system: True`). Long
  agent loops on the official API without it re-pay the full prompt every
  turn.
- **`trust_env` defaults to off** — deliberate, so ambient proxy settings
  can't silently reroute provider traffic. In proxied environments set
  `LOVIA_PROVIDER_TRUST_ENV=1` or nothing connects.
- **Providers built from strings are run-owned; instances are yours.**
  A `Provider` you construct and pass in is never closed by the runner —
  reuse it across runs, close it yourself.
- **`supports_json_schema` inference follows the host.** A compatible
  endpoint that *does* support native JSON schema needs the explicit
  constructor flag to get the native path.
- **Anthropic server tools can pause a turn.** Enabling a server tool (web
  search, code execution) through `provider_options` may end a long turn
  with `stop_reason: "pause_turn"` — the API's request to re-send the
  conversation and continue. lovia does not auto-continue yet: the turn
  ends with its partial content, and the raw `pause_turn` finish reason is
  passed through so callers can detect it.

## See also

- [Structured output](structured-output.md) — native vs prompt-path schemas
- [Provider retries](retries.md) — transient-failure handling
- [Context management](context.md) — how windows and caching interact
- Examples: [`09_model_settings.py`](../../examples/09_model_settings.py),
  [`10_custom_provider.py`](../../examples/10_custom_provider.py)
