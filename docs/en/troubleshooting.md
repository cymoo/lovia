# Troubleshooting

Start with the exception type and its `.hint`; lovia errors are designed to
say whether the caller, Provider, Tool, or runtime boundary failed.

For interactive diagnosis, enable framework logs:

```python
from lovia import enable_logging

enable_logging("DEBUG")
```

Do not include API keys, full prompts, private Tool results, or environment
dumps in bug reports.

## No model is configured

**Symptoms:** `UserError` before the first Turn, usually because the Agent has
no model or the model string is invalid.

Pass a valid `model=` to the Agent, or set `LOVIA_MODEL` and read it with
`model_from_env()`; either way, provide the credentials/Base URL for your
endpoint. Remember that the library does not load `.env` automatically. Follow
[Installation](installation.md#configure-a-model).

## Provider authentication or endpoint failures

| Symptom | Check |
| --- | --- |
| HTTP 401/403 | API key belongs to this endpoint and was exported in the current process |
| HTTP 404 | Base URL includes the endpoint's required prefix, commonly `/v1` |
| Anthropic payload rejected by a compatible endpoint | Model uses `anthropic:` and the service is configured through `ANTHROPIC_BASE_URL` |
| OpenAI payload rejected by a compatible endpoint | Model is bare or `openai:` and the service is configured through `OPENAI_BASE_URL` |
| Native JSON Schema rejected | Pass `supports_json_schema=False` for a compatible OpenAI endpoint that needs the Prompt fallback |

Inspect `ProviderError.status_code`, `.vendor`, `.model`, and `.retryable` when
available. Endpoint dialect rules are in [Providers & models](providers.md).

## Proxies and TLS

Provider clients ignore ambient `HTTP_PROXY` / `HTTPS_PROXY` by default. Set
`LOVIA_PROVIDER_TRUST_ENV=1` when those variables are intentional.

For a private CA, use `LOVIA_HTTP_CA_BUNDLE=/path/to/ca.pem`. The Web extra
also enables the operating-system trust store. `LOVIA_HTTP_INSECURE=1`
disables verification and should be restricted to short-lived local diagnosis,
never production.

## Context overflow or missing early instructions

- `ContextOverflowError`: set the known `context_window`, reduce output
  reservation, or inspect custom compaction stages.
- Ollama silently loses the oldest content instead of raising: configure
  `Compaction(context_window=...)` to match `num_ctx`.
- Huge Tool results: reduce them at the source or lower
  `max_tool_output_chars`; compaction only shrinks the View, not the stored
  Transcript.

See [Context management](context.md) and
[Provider context windows](providers.md#context-windows).

## A Tool was not called

Check four things:

1. The Tool is attached to the Agent and its name is unique.
2. Its docstring describes **when** to use it, not only what it returns.
3. The model supports Tool calling and received the Tool Schema.
4. Instructions do not tell the model to avoid the action.

Use `ScriptedProvider.calls` in an offline test to inspect the exact View. If a
Tool ran but failed, ordinary exceptions are returned to the model as Tool
results; `RunCancelled` and Run-level `BudgetExceeded` still end the Run.

## Structured output does not validate

Keep Schemas shallow and explicit, inspect `OutputValidationError.raw`, and
check whether the endpoint uses native JSON Schema or Prompt fallback. A repair
attempt consumes another Turn. See [Structured output](structured-output.md).

## A Run repeats old output or ignores new input

A completed checkpoint `run_id` is an idempotency key: reusing it replays the
stored result and ignores the new input. Use a new `run_id` for new work and a
stable `session_id` for conversation continuity. See
[Sessions & checkpoints](sessions-and-checkpoints.md#run_id-is-an-idempotency-key).

## Streaming ended without raising

This is the contract. Event iteration ends with `RunFailed`; the exception is
raised by `await handle.result()`. Always await the result after consuming the
stream.

## The Web server is unreachable or unsafe to expose

- Default bind: `127.0.0.1:8000`; remote machines cannot reach Loopback.
- There is no built-in authentication or rate limiting.
- Use one worker; live Run supervision and approvals are process-local.
- Disable or restrict a writable Workspace before exposing the server.

See [Deployment](deployment.md) before changing the bind address.

## Optional dependency errors

Install the matching extra:

```bash
pip install "lovia[mcp]"   # MCP
pip install "lovia[ddg]"   # DuckDuckGo search
pip install "lovia[web]"   # FastAPI server and UI
```

## Reporting a useful issue

Include:

- `lovia.__version__` and Python version
- Exception type, message, `.hint`, and chained cause
- Provider family and whether the endpoint is official or compatible
- A minimal reproducer using `ScriptedProvider` when possible
- Sanitized logs and the relevant configuration names, never secret values
