# AGENTS.md

Instructions for AI coding assistants working on this repository.

## Project overview

**lovia** is a lightweight, provider-neutral agent framework for Python.

## Setup

```bash
pip install -e .[dev]
# Optional MCP support
pip install -e .[mcp]
# Optional web layer (FastAPI + chat UI)
pip install -e .[web]
```

Requires Python 3.10+. Use the repository virtualenv for Python commands:
`.venv/bin/python -m pytest`, `.venv/bin/python -m ruff`, etc.

## Commands

| Task | Command |
| --- | --- |
| Run all tests | `pytest` |
| Run a single test | `pytest tests/test_runner.py::test_plain_text_run` |
| Run with coverage | `pytest --cov=lovia --cov-report=term-missing` |
| Lint | `ruff check .` |
| Format | `ruff format .` |
| Type-check | `mypy lovia` |

Tests use `pytest-asyncio` with `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed). Tests that call real LLM endpoints are gated behind `@pytest.mark.live_provider` and skipped by default.

## Architecture

```
lovia/
  agent.py          # Agent dataclass — main user-facing config
  runner.py         # Runner — thin public facade (stateless class methods)
  runtime/          # *** The real orchestration lives here ***
    loop.py         #   RunLoop — the only module with mutable state
    model_turn.py   #   Calls the provider, assembles deltas → AssistantTurn
    tool_calls.py   #   Dispatches tool calls, handoff, approval, final_output
    run_state.py    #   RunState (mutable per-run) + ActiveAgent (per-agent derived state)
    checkpoint.py   #   CheckpointWriter
    result.py       #   RunHandle (async iterator + awaitable) and RunResult
  tools/            # @tool decorator, Tool type, and opt-in tool factories
    base.py         #   core Tool/tool API
    files.py        #   workspace-backed file tools
    shell.py        #   workspace shell tool
    http.py         #   http_fetch
    search.py       #   duckduckgo_search_tool  (requires lovia[ddg])
    human.py        #   HumanChannel + ask_human
    recall.py       #   recall_tool_result (recovers compacted tool outputs)
    time.py         #   now
  messages.py       # Message, ToolCall, Usage types — lossy chat-provider view
  transcript.py     # TranscriptEntry — canonical discriminated union; conversions;
                    #   safe_window() pair-aware slicing
  events.py         # Streaming event types
  output.py         # Structured output handling (native JSON Schema / final_output fallback)
  handoff.py        # Handoff + agent_as_tool
  hooks.py          # AgentHooks subscriber (handlers called as handler(event, ctx))
  guardrails.py     # input/output guardrail protocol
  session.py        # Session protocol
  context/          # Context-window management (see "Context compaction" below)
    policy.py       #   ContextPolicy protocol, CompactionRequest/ContextResult, Noop
    compaction.py   #   Compaction — the default policy (sticky staged pipeline)
    stages.py       #   Stage protocol + OffloadToolResults/ClearToolResults/SummarizeHistory
    state.py        #   CompactionState (sticky decisions) + transcript fingerprint
    render.py       #   pure transcript+state → view rendering, markers, protected tail
    tokens.py       #   TokenCounter (memoized estimates) + TokenBudget (watermarks)
    summarizer.py   #   Summarizer protocol + LLMSummarizer (structured sections)
    prompts.py      #   summary prompt templates + background-reference wrapper
  plugins/          # Declarative capability plugins — the one extension axis
    base.py         #   Plugin protocol (async setup + aclose) + PluginInstance
                    #   (tools/instructions/view_injectors/hooks/guardrails)
    todo.py         #   Todo plugin: todo_write + per-turn reminder injector
    skills.py       #   Skills plugin + SkillCategory/SkillSource (SKILL.md disclosure)
    mcp.py          #   MCP plugin + MCP client (lazy; requires mcp package)
  schema.py         # JSON Schema generation from Python types
  exceptions.py     # Framework exceptions (carry an optional .hint)
  providers/        # LLM provider adapters (OpenAI, Anthropic, …)
  stores/           # Session and memory store implementations
  workspace/        # Filesystem + process workspace (Workspace.local,
                    #   WorkspaceLike/WorkspaceSession protocols, policy gating)
  web/              # Optional FastAPI + SSE layer + Jinja2 chat UI
                    #   (decoupled from core; only loaded when lovia[web] is used)
```

Three layers, each strictly downstream of the previous: **core** (everything
outside `workspace/` and `web/`), **workspace** (fs + exec), **web** (HTTP/SSE/UI).
Core never imports workspace or web (type-only imports excepted).

### Runner split

`lovia/runner.py` is a **thin public facade** (class methods only, stateless). The real orchestration lives in `lovia/runtime/loop.py` — `RunLoop` is the only module in the framework that owns mutable state. When tracing how a run executes, start at `RunLoop.stream()` → `_stream_inner()`, not `Runner.run()`.

### Two transcript representations

- **`TranscriptEntry`** (in `transcript.py`) is the **canonical** form. It's a discriminated union of dataclasses — `InputEntry`, `AssistantTextEntry`, `ReasoningEntry`, `ToolCallEntry`, `ToolResultEntry` — using a `type: Literal[...]` discriminator. This is what the runner loop, sessions, and checkpointer operate on.
- **`Message`** (in `messages.py`) is a **lossy**, chat-provider-shaped view (`system`/`user`/`assistant`/`tool` roles). `RunResult.messages` is derived via `entries_to_messages()`, not authoritative.

Conversion functions: `entries_to_messages()`, `messages_to_entries()`, `input_to_entries()`, `assistant_to_entries()`.

### Provider protocol

`Provider` is a `Protocol` (not an ABC) in `providers/base.py`. Each provider:
1. Receives `list[TranscriptEntry]` (not `list[Message]`)
2. Yields `ModelDelta` values — `TextDelta`, `ReasoningDelta`, `ToolCallDelta`, `UsageDelta`, `FinishDelta`, `EntryCompletedDelta`
3. Declares `supports_json_schema` (controls whether structured output uses native `response_format` or the `final_output` tool fallback)

Provider registration supports the `lovia.providers` entry-point group for third-party adapters. Built-in prefixes: `openai` (aliases `openai-chat`, `oai`), `anthropic` (alias `claude`).

### Tool merging

Tools from several sources are merged in `RunLoop._collect_tools()` with name-conflict detection: `agent.tools`, plugins (which now include MCP, skills, and todos), `agent.workspace`, handoffs, and the synthetic `final_output` tool (when structured output falls back to tool mode).

### Plugins and view injectors

A `Plugin` (`plugins/base.py`) is the framework's one extension axis for bundled capabilities — `MCP`, `Skills`, and `Todo` are all built-in plugins under `plugins/`. `RunLoop._activate_plugins()` `await`s `plugin.setup()` **once per run** (and once per agent on a handoff), so run-scoped state (and async resources like MCP connections) built inside `setup` is fresh and concurrency-safe; each instance's `aclose` is registered for LIFO teardown when the run ends. The returned `PluginInstance` contributes across fixed loop slots: `tools` (merged above), `instructions` (folded into `_system_prompt`), `view_injectors` (per-turn, below), `hooks` (dispatched alongside `agent.hooks` in `_emit`; each handler is called `handler(event, ctx)` with the live `RunContext`, like guardrails/view-injectors), and `input_guardrails`/`output_guardrails` (run at the loop's existing checkpoints, merged with the agent's own — the loop keeps the abort). Plugins hold no control flow of their own.

`ViewInjector`s are the one **per-turn** seam: `RunLoop._augment_view()` runs them after `_build_view()` in `_model_phase` and appends their transient entries to the tail of the per-call view **only** — never to `state.transcript` or the `Session`. So the injected content (e.g. the todo reminder) neither accumulates as turns grow nor changes the cached system-prompt prefix. Injectors are fail-open: a raising injector is logged and skipped, never aborting the run. The todo plugin (`plugins/todos/`) is the first consumer; the same seam is the primitive for ephemeral message insertion generally.

### Handoff mechanism

Handoffs use a **sentinel pattern** across three modules. When a handoff tool is invoked (`transfer_to_<name>`):

1. `handoff.py:build_handoff_tool()` — the tool's invoke returns a `_HandoffSignal(handoff=...)` dataclass (carrying the per-call `reason`) instead of a normal result.
2. `runtime/tool_calls.py:ToolCallProcessor.process()` — detects `_HandoffSignal` via `isinstance()`, sets `state.pending_handoff`, and writes a text result to the transcript.
3. `runtime/loop.py:RunLoop._apply_handoff()` — after the tool calls are processed, the loop checks `state.pending_handoff`; if set, it resolves a fresh `ActiveAgent` for the target via `_resolve_active()` (its own providers, tools, structured output, workspace, and plugin contributions) and swaps it in atomically with `RunState.activate()`, then rewrites the leading system message via `_reset_transcript_for_handoff()`.

This keeps the runner's main loop simple: handoff is just another tool result, flagged with a sentinel type. The optional `Handoff.input_filter` rewrites the transcript body as `TranscriptEntry` objects (not flattened `Message`s), so reasoning, server-side tool calls, and provider metadata survive the rewrite. The run-level `extra_instructions` addendum is re-applied to every agent reached by a handoff.

### Session vs Checkpointer

Two persistence concepts that serve different purposes. The model: **`session` = the log of completed runs; the checkpoint = the log of the in-flight run; the full transcript = `session.load() + snapshot.entries`.** Both stores are append-only and symmetric.

- **`Session`** (`session.py`) — the conversation transcript keyed by `session_id`, for multi-turn chat. **Append-only** (`load` / `append` / `clear`, no `replace`): the runner loads history at the start and, when a run finishes, `append`s that run's **own** entries as one segment keyed by `run_id` (generated when absent — most session-only runs have no checkpoint `run_id`). `SQLiteSession` stores one row per run in `session_runs` with `UNIQUE(session_id, run_id)`, so append is an idempotent `INSERT OR IGNORE` (a re-issued run never duplicates) and an old row is never rewritten; `load` concatenates in insertion order. Prior history is immutable — that's what lets resume safely reload it. Context compaction never writes to the Session.
- **`Checkpointer`** (`checkpointer.py`) — the in-flight run keyed by `run_id`. **Append-only** and symmetric with the Session: `append(run_id, entries, head)` adds a turn's entries and overwrites a small mutable `RunHead` (usage + turns + status + `agent_name` + last-input-tokens + context-policy-state). `RunSnapshot = run_id + entries + head`, where `entries` is the run's **own** entries (not the prior history). `SQLiteCheckpointer` keeps one row per turn (`snapshot_turns`) + one head row (`snapshot_heads`); the loop appends after the model output and after each tool call via `CheckpointWriter.save_running()`. On success the run's entries are appended to the Session **after** the checkpoint is finalized (`_stream_inner`), so a crash between the two can't leave the run both persisted *and* resumable (which would double-count on resume). `agent_name` records the *active* agent — after a handoff that is the target, not the entry agent. On resume `RunLoop` resolves that agent by name from the entry agent's handoff graph (`runtime/resume.py:resolve_resume_agent`), reloads history from the Session, and rebuilds the run as that agent, so multi-agent runs resume correctly.

Long-term cross-session **memory** is deliberately *not* a core runtime primitive — there is no `Memory` protocol baked into the loop or `Agent.memory` field. It ships instead as a first-class **plugin** (`Memory`, in `plugins/memory.py`), built entirely on existing plugin seams (injected instructions, tools, and a `RunCompleted` hook that reads `session_id` and the active agent off the `RunContext` passed to every handler); see the **Memory** section in the README. The same seams let you wire your own memory over a custom store.

### Context compaction

`ContextPolicy` implementations produce the **per-call view** of the transcript
sent to the provider. Compaction is view-only: it never mutates the transcript
or the `Session`, so the full conversation stays the source of truth. A single
method handles both triggers:
- **Proactive**: `policy.compact(req)` runs before each model turn.
- **Reactive**: on `ContextOverflowError`, the runner sets `req.overflow=True`
  and calls `compact` again for a more aggressive view, then retries the turn
  once (only when the policy reports `compacted=True`, i.e. it made *new*
  decisions).

`Runner` defaults to `Compaction` (in `context/compaction.py`); pass
`NoopContextPolicy()` to disable. Key design points, in dependency order:

- **Plan/render split.** Stages never transform views. They record *sticky
  decisions* into `CompactionState` (cleared call_ids, offloaded
  call_id→file-path records, running-summary text + coverage), and the pure
  function `render_view(transcript, state)` rebuilds the per-call view.
  Decisions are monotonic, so the rendered prompt prefix is byte-stable
  across turns — that is what keeps provider prompt caches warm. Never make a
  stage "undo" a decision.
- **Watermark hysteresis.** Nothing happens below `compact_at` (default 0.75
  of the usable window); a burst then shrinks the view to `compact_to`
  (default 0.50). Both accept a fraction (float) or absolute tokens (int).
  `TokenBudget` owns the math; `reserve_output_tokens` is subtracted first.
- **Cheap-first stages**: `OffloadToolResults` (archive huge results to
  workspace files; inert without a writable workspace) → `ClearToolResults`
  (replace older results with recall markers; Anthropic `clear_tool_uses`
  semantics) → `SummarizeHistory` (incremental LLM summary of the older
  prefix; anti-thrash skip below 10% projected savings; per-run circuit
  breaker). Custom stages implement the `Stage` protocol
  (`async def plan(body, ctx) -> bool`).
- **Protected tail.** `render.protected_tail_start()` computes the verbatim
  tail every stage must respect: token-budgeted (`keep_recent_tokens`,
  default usable//5), anchors the most recent user message when affordable,
  and expands over tool call/result pairs so views never contain orphan
  results. On the aggressive path, a single result bigger than the target
  budget loses this immunity (`_oversized` in `stages.py`) — otherwise one
  giant tool output would make overflow recovery impossible.
- **Token accounting.** `TokenCounter` estimates per entry (chars//4, flat
  image/file costs, `id()`+weakref memo) and is *calibrated* against the
  provider's real `last_input_tokens` via an EMA ratio stored in state.
- **State location.** Sticky state serializes into the per-run
  `ResumeState.compaction_scratch` (JSON-safe → survives checkpoint/resume).
  `Compaction` additionally keeps a bounded in-process cache keyed by
  `session_id` so a *new run* on the same session resumes prior decisions; a
  structural `fingerprint` of the covered prefix detects rewritten history
  (handoff `input_filter`) and resets the summary while keeping
  call_id-keyed decisions.
- **Markers and recovery.** Cleared/offloaded results render as markers that
  preserve `call_id`/`is_error` (pair validity). Markers mention the opt-in
  `lovia.tools.recall_tool_result` tool only when the agent actually has it
  (`CompactionRequest.tool_names`); offload markers carry the file path +
  preview. The full output always remains in the real transcript.
- **Memory is bounded at the transcript boundary, not by compaction.**
  Compaction shapes only the per-call *view*; the transcript keeps full tool
  outputs (plus `ToolResultEntry.raw`) for the run's lifetime, and sessions/
  checkpoints persist them. Tools that can return huge payloads should be
  capped at the source: built-in workspace tools already truncate
  (`max_read_chars`/`max_output_chars` on `Workspace`), and user tools are
  capped via `Agent.max_tool_output_chars` or per-tool
  `@tool(max_output_chars=...)` — `ToolCallProcessor` truncates (head + tail
  + marker) before the entry is stored and drops the raw value. This is
  deliberately lossy; `recall_tool_result` sees the truncated version.

`safe_window()` in `transcript.py` is critical for any policy that drops middle entries — it ensures `ToolCallEntry`/`ToolResultEntry` pairs stay intact by walking the cut point backward to include orphaned call IDs.

## Testing conventions

Tests use `ScriptedProvider` (in `tests/scripted_provider.py`) — a deterministic, in-memory provider that replays pre-canned `AssistantTurn` objects. No network calls. Build scripts with the `text()` and `call()` helpers:

```python
from .scripted_provider import ScriptedProvider, call, text

provider = ScriptedProvider([
    call("add", {"a": 2, "b": 3}, call_id="c1"),
    text("The answer is 5."),
])
```

The provider records every prompt it receives in `provider.calls` (as `list[list[Message]]`), so tests can assert on what the agent actually sent.

Context-system tests live under `tests/context/` (tokens, state, render,
stages, pipeline, recall, offload integration). Live end-to-end tests against
the real endpoint configured in `.env` are in
`tests/context/test_live_context.py` and `tests/providers/test_live.py`; run
them with `LOVIA_LIVE_TESTS=1 pytest -m live_provider` (the genuine
context-overflow probe additionally needs `LOVIA_LIVE_OVERFLOW_TESTS=1`).

## Conventions

- **Agent is immutable** — dataclass with `clone(**overrides)`; never mutate in place. The `_fragments` tuple is the only non-public field and is copied immutably on clone.
- **Async-only** public API. All runner methods are `async`; `run_sync()` wraps `asyncio.run()` — never duplicate logic across sync and async code paths.
- **Pydantic v2** for data models. Prefer `model_fields`, `model_validate`, etc.
- **Type annotations** on all public functions and classes.
- **Errors carry `.hint`** — every `LoviaError` subclass accepts an optional `hint=` kwarg rendered in `str(exc)`. `OutputValidationError` also exposes the raw model text and target schema name. Bury no context.
- Keep the core minimal — hard dependencies are `httpx` + `pydantic` only. Every other capability (MCP, web, search, Prefect) is an opt-in extra.
- Provider adapters live under `lovia/providers/`; each adapter translates between the lovia transcript format and the vendor API.
- Follow `ruff` rules for formatting and linting. Do not add `# noqa` suppressions without a comment explaining why.
- **Backwards compatibility** — renames go through a deprecation shim for at least one minor release.

## Design philosophy

lovia is built around four words. When in doubt, optimise for the one earlier in the list.

1. **Concise (简洁).** Every piece should fit on one screen of mental model. The core (`agent.py`, `runner.py`, `tools/`, `output.py`, `schema.py`, `skills.py`, `exceptions.py`) stays small and obvious. New features must justify their line cost; cleverness that saves keystrokes but obscures behaviour is rejected.
2. **Lightweight (轻量).** Core has exactly two hard dependencies: `httpx` and `pydantic`. Every other capability — MCP, web UI, DuckDuckGo, etc. — is an opt-in extra and only imported when the user asks for it. `import lovia` must stay cheap.
3. **Extensible (易扩展).** Public surfaces are dataclasses, Protocols, and `@decorator` hooks — not subclasses you must inherit from. Providers, sessions, memory stores, web-search backends, and hooks are all Protocol-based; users plug in their own implementations without monkey-patching.
4. **General-purpose (通用).** `lovia.tools.*` ships practical, framework-agnostic tools (http, search, human-in-the-loop, time, filesystem, shell), `lovia.plugins.*` ships the built-in plugins (todos, skills, MCP), and `lovia.workspace.*` ships the filesystem + process boundary so a real agent can be assembled in minutes. Optional integrations such as web, Rich examples, and Prefect examples stay behind extras.

A few corollaries that follow from these:

- **Decline before designing.** If a feature looks indispensable, check whether it can be a 10-line user-side recipe instead of a framework abstraction.
- **Backwards compatibility is a feature.** Renames go through a deprecation shim for at least one minor release.

## Git commit convention

Follow [Conventional Commits](https://www.conventionalcommits.org/): `type(scope): imperative summary`.
Common types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`.
