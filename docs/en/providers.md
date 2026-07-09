# Providers & models

lovia is provider-neutral without an adapter tax: two built-in providers
speak OpenAI Chat Completions and Anthropic Messages directly over `httpx`,
any OpenAI-compatible endpoint rides the first one, and a custom vendor is a
`Protocol` implementation — not a subclassing project.

```python
from lovia import Agent, ModelSettings

agent = Agent(
    name="assistant",
    model=["anthropic:<model>", "glm-5.2"],  # fallback chain
    settings=ModelSettings(temperature=0.2, max_tokens=800),
)
```

## Model strings

`Agent(model=...)` accepts a `"vendor:model"` string, a `Provider` instance,
or a list of either (a [fallback chain](#fallback-chains)).

| Prefix | Provider | Aliases |
| --- | --- | --- |
| `openai:` | OpenAI Chat Completions | `oai:`, `openai-chat:` |
| `anthropic:` | Anthropic Messages | `claude:` |
| *(none)* | OpenAI Chat Completions | — |

A **bare name** (`"glm-5.2"`) routes to the OpenAI-compatible
provider — the intended spelling for `OPENAI_BASE_URL` services. One guard:
a bare name starting with `claude` logs a warning, since it is almost always
a missing `anthropic:` prefix. There is deliberately no default model:
running an agent without one raises `UserError`.

To avoid hard-coding models in scripts, `model_from_env()` reads (in order)
`LOVIA_MODEL`, `OPENAI_DEFAULT_MODEL`, `ANTHROPIC_DEFAULT_MODEL`, raising
with a setup hint when none is set (`required=False` returns `None` instead;
a bare `ANTHROPIC_DEFAULT_MODEL` gets the `anthropic:` prefix
automatically).

## The OpenAI provider

`OpenAIChatProvider(model, *, api_key=None, base_url=None, client=None,
timeout=None, default_headers=None, supports_json_schema=None,
trust_env=None, replay_reasoning=None, official_api=None)`

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
`official_api=` overrides it.

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
default_headers=None, trust_env=None, official_api=None)`

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

## Fallback chains

`model=[...]` lists providers in preference order. The runner works through
the chain on provider errors — a retryable failure first exhausts the
current provider's [retry policy](reliability.md#provider-retries), then the
next provider takes over. One capability note: with a mixed chain,
[structured output](structured-output.md) uses the native path only when
**every** provider in the chain supports it — otherwise a mid-run fallback
would reject the schema payload — so a chain mixing capabilities quietly
uses the prompt path for all.

```python
agent = Agent(name="assistant", model=["anthropic:<model>", "glm-5.2"])
```

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
| Explicit config | `Compaction(context_window=…)`, `--context-window`, `LOVIA_CONTEXT_WINDOW`, or the adapter's `context_window=` argument |
| **The endpoint's own rejection** | after one overflow: providers name the limit ("maximum context length is 65536 tokens") |
| **The endpoint's `/models` listing** | vLLM and SGLang publish `max_model_len`; Groq, Together and OpenRouter publish theirs |
| The bundled table | recent OpenAI aliases; the whole Claude line via family prefixes |
| — | otherwise unknown: no proactive compaction, reactive overflow handling only |

Only the second source may *override* an explicit setting, and only downward —
it is the endpoint enforcing a limit, not a guess. Configure `100_000` on a 200K
model and you keep 100K.

The practical upshot: an unlisted model costs **one** overflow, once. The window
learned from it is remembered on the provider and persisted in the session, so
every later turn (and every later run on that session) sizes itself correctly.

Two consequences worth knowing:

- **The `/models` probe fires only when nothing else knows the window**, and
  never against `api.openai.com` or `api.anthropic.com`, which publish nothing
  there. A miss is cached, so an endpoint is asked at most once.
- **Anthropic models report 200K.** The 1M variants sit behind a beta header
  lovia doesn't send by default, and advertising 1M would delay proactive
  compaction. Enable the beta and set the window explicitly.

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
provider a first-class citizen of compaction: `ContextWindowProvider`
(report the window locally, no I/O), `ContextWindowDiscovery` (an async
one-shot lookup against the endpoint, called only when nothing else knows),
and `TokenEstimator` (better-than-heuristic counting).
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
| `LOVIA_PROVIDER_TIMEOUT` | request timeout, seconds | `60` |
| `LOVIA_PROVIDER_TRUST_ENV` | honor `HTTP(S)_PROXY` / `NO_PROXY` | off |
| `LOVIA_HTTP_CA_BUNDLE` | custom PEM bundle for outbound TLS | — |
| `LOVIA_HTTP_INSECURE` | disable certificate verification | off |

Constructor arguments (`timeout=`, `trust_env=`) win over the environment.
TLS verification resolves in order: `LOVIA_HTTP_INSECURE` → the CA bundle →
the OS trust store when the optional `truststore` package is installed
(bundled with `lovia[web]`) → `certifi`. The same resolution covers the
[`http_fetch` tool](built-in-tools.md#http-fetch), so one intranet-CA
setting fixes every outbound request.

**Error classification** feeds the [retry machinery](reliability.md):
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
  tell you. Set `Compaction(context_window=…)` or
  `OpenAIChatProvider(..., context_window=…)` to match your `num_ctx`.
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

## See also

- [Structured output](structured-output.md) — native vs prompt-path schemas
- [Reliability](reliability.md) — retries and fallback in depth
- [Context management](context.md) — how windows and caching interact
- Examples: [`09_model_settings.py`](../../examples/09_model_settings.py),
  [`10_custom_provider.py`](../../examples/10_custom_provider.py)
