# lovia internals

Contributor-facing notes on how the framework is built. [AGENTS.md](../AGENTS.md)
is the tight entry point; this file is the deep reference it links to. Read the
relevant section before modifying the subsystem it describes — these document
non-obvious invariants you can't recover from a single file.

## Module map

```
lovia/
  agent.py          # Agent dataclass — main user-facing config
  runner.py         # Runner — thin public facade (stateless class methods)
  runtime/          # *** The real orchestration lives here ***
    loop.py         #   RunLoop — the only module with mutable state
    model_turn.py   #   Calls the provider, assembles deltas → AssistantTurn
    tool_calls.py   #   Tool-call preflight (gates/approval) + execute (parallel-capable)
    run_state.py    #   RunState (mutable per-run) + ActiveAgent (per-agent derived state)
    checkpoint.py   #   CheckpointWriter
    result.py       #   RunHandle (async iterator + awaitable) and RunResult
  tools/            # @tool decorator, Tool type, and opt-in tool factories
    base.py         #   core Tool/tool API
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
  steering.py       # Mailbox — mid-run user-message injection, drained at turn
                    #   starts; always present on RunContext (ctx.mailbox)
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
  eval/             # Declarative evals: Case + checks → evaluate() → Report
    checks.py       #   Check protocol (any (RunResult) -> CheckResult | bool)
                    #   + deterministic matchers + all_of/any_of/weighted
    judge.py        #   llm_judge — an Agent(output_type=Verdict) underneath
    runner.py       #   Case + evaluate() (sampling, concurrency, fail_fast)
    report.py       #   SampleResult/CaseResult/Report + baseline diff
  testing.py        # ScriptedProvider + text()/call() — public deterministic fake
  schema.py         # JSON Schema generation from Python types
  exceptions.py     # Framework exceptions (carry an optional .hint)
  providers/        # LLM provider adapters (OpenAI, Anthropic, …)
  stores/           # Session and memory store implementations
  workspace/        # Filesystem + process workspace
    workspace.py    #   Workspace.local factory + LocalWorkspace config
    policy.py       #   WorkspacePolicy — one allow/ask/deny ACL for paths
                    #   (PathRule) and shell commands (CommandRule)
    paths.py        #   resolve_path — resolves (symlinks followed) and
                    #   classifies inside/outside the root; no path is
                    #   rejected on syntax, the ACL judges the target
    local.py        #   LocalWorkspaceSession — the single enforcement point
                    #   (deny raises here; ask is gated at the tool layer)
    command_guard.py#   lexical path-claim extraction from shell commands
    tools.py        #   read_file/write_file/edit_file/list_files/grep_files/
                    #   shell + needs_approval predicates (the ask side)
    protocol.py     #   WorkspaceSession/WorkspaceLike/ShellExecutor protocols
  web/              # Optional FastAPI + SSE layer + Jinja2 chat UI
                    #   (decoupled from core; only loaded when lovia[web] is used)
```

Three layers, each strictly downstream of the previous: **core** (everything
outside `workspace/` and `web/`), **workspace** (fs + exec), **web** (HTTP/SSE/UI).
Core never imports workspace or web (type-only imports excepted).

## Runner split

`lovia/runner.py` is a **thin public facade** (class methods only, stateless). The real orchestration lives in `lovia/runtime/loop.py` — `RunLoop` is the only module in the framework that owns mutable state. When tracing how a run executes, start at `RunLoop.stream()` → `_stream_inner()`, not `Runner.run()`.

## Two transcript representations

- **`TranscriptEntry`** (in `transcript.py`) is the **canonical** form. It's a discriminated union of dataclasses — `InputEntry`, `AssistantTextEntry`, `ReasoningEntry`, `ToolCallEntry`, `ToolResultEntry` — using a `type: Literal[...]` discriminator. This is what the runner loop, sessions, and checkpointer operate on.
- **`Message`** (in `messages.py`) is a **lossy**, chat-provider-shaped view (`system`/`user`/`assistant`/`tool` roles). `RunResult.messages` is derived via `entries_to_messages()`, not authoritative.

Conversion functions: `entries_to_messages()`, `messages_to_entries()`, `input_to_entries()`, `assistant_to_entries()`.

## Provider protocol

`Provider` is a `Protocol` (not an ABC) in `providers/base.py`. Each provider:
1. Receives `list[TranscriptEntry]` (not `list[Message]`)
2. Yields `ModelDelta` values — `TextDelta`, `ReasoningDelta`, `ToolCallDelta`, `UsageDelta`, `FinishDelta`, `EntryCompletedDelta`
3. Declares `supports_json_schema` (controls whether structured output uses native `response_format` or the `final_output` tool fallback)

Provider registration supports the `lovia.providers` entry-point group for third-party adapters. Built-in prefixes: `openai` (aliases `openai-chat`, `oai`), `anthropic` (alias `claude`).

## Tool merging

Tools from several sources are merged in `RunLoop._collect_tools()` with name-conflict detection: `agent.tools`, plugins (which now include MCP, skills, and todos), `agent.workspace`, handoffs, and context-policy tools such as `recall_tool_result` (added last; shadowed silently by an explicit tool of the same name). Structured output never contributes a tool — the non-native fallback lives in the system prompt (`output.py:format_output_instructions`).

## Parallel tool execution

One turn's tool calls run **concurrently by default**. `RunLoop._tool_phase` splits each call in two (`runtime/tool_calls.py`): `preflight()` — cancel/budget checks, tool lookup, handoff dedup, argument parsing, and the approval flow — runs serially in request order on the loop's generator body (so approval backpressure and budget determinism are exactly the serial semantics), while ready calls execute in background tasks (`ToolCallProcessor.execute()`) whose events funnel through one `asyncio.Queue` back onto that body. That drain point is the single place events reach hooks and the stream consumer and checkpoints are saved — hook ordering and the per-result durability cadence survive unchanged. Results append to the transcript in completion order, which is safe because every consumer pairs calls to results by `call_id`, never by position.

`Tool.parallel=False` (`@tool(parallel=...)`) opts a tool out: it becomes an **execution barrier** — in-flight calls finish, the tool runs alone, then dispatching resumes. Handoff tools are always barriers (whatever their flag), which keeps first-handoff-wins race-free; the built-in workspace mutators (`write_file`, `edit_file`, `shell`) default to `parallel=False` so filesystem/process side effects never race within a turn. A `BudgetExceeded` from preflight stops dispatching but drains in-flight calls to completion (the `RunBudget` contract); `RunCancelled` and checkpoint-store failures cancel the in-flight siblings promptly — their dangling calls are re-executed by a resume, exactly like a serial abort's not-yet-run calls.

## Workspace permission model

One three-valued ACL (`allow`/`ask`/`deny`) governs both files and shell. The split invariant:

- **`deny` is enforced in the session** (`LocalWorkspaceSession`): every file op resolves its path (symlinks followed, absolute/`~`/relative all accepted) and asks `WorkspacePolicy.decide_path(rel, abs, op)`; `run()` asks `session.decide_command(command, cwd)` (static `command_rules` merged most-restrictive with path claims lexically extracted by `command_guard.py` — redirect targets are writes, path-looking args are reads). Custom tools calling the session directly get the same gate.
- **`ask` is resolved at the tool layer**: the built-in tools carry `needs_approval` predicates that call `session.decide_path`/`decide_command` and route through the existing `ApprovalRequired` channel. By the time a call reaches the session, `ask` has been approved — the session lets it pass. Bulk operations (list/grep) never trigger approval mid-walk; anything short of `allow` for a symlinked file inside the walk is skipped, and only the operation's own target can ask.
- Symlinks have no special case: a path is judged by where it resolves. Inside-root reads are always `allow`; `write`, `read_outside`, `write_outside` and `path_rules`/`denied_paths` cover the rest. Presets: `readonly`, `coding` (outside reads ask, outside writes deny), `trusted` (reads anywhere, outside writes ask).
- The command guard is **advisory** (it cannot see `python -c` or `$(...)` payloads) but one-sided: a missed claim falls back to the static rules, and a false claim is almost always a relative token that resolves inside the root where reads are allowed — it can surface extra `ask`s, not loosen anything. Hard enforcement is the `ShellExecutor` seam (`protocol.py`): an executor derives OS sandbox scopes (Seatbelt/bubblewrap) from the policy and runs *after* the policy/approval gates.
- `instructions()` is generated from the policy so the system prompt never promises more than the session enforces.

## Plugins and view injectors

A `Plugin` (`plugins/base.py`) is the framework's one extension axis for bundled capabilities — `MCP`, `Skills`, and `Todo` are all built-in plugins under `plugins/`. `RunLoop._activate_plugins()` `await`s `plugin.setup()` **once per run** (and once per agent on a handoff), so run-scoped state (and async resources like MCP connections) built inside `setup` is fresh and concurrency-safe; each instance's `aclose` is registered for LIFO teardown when the run ends. The returned `PluginInstance` contributes across fixed loop slots: `tools` (merged above), `instructions` (folded into `_system_prompt`), `view_injectors` (per-turn, below), `hooks` (dispatched alongside `agent.hooks` in `_emit`; each handler is called `handler(event, ctx)` with the live `RunContext`, like guardrails/view-injectors), and `input_guardrails`/`output_guardrails` (run at the loop's existing checkpoints, merged with the agent's own — the loop keeps the abort). Plugins hold no control flow of their own.

`ViewInjector`s are the one **per-turn** seam: `RunLoop._augment_view()` runs them after `_build_view()` in `_model_phase` and appends their transient entries to the tail of the per-call view **only** — never to `state.transcript` or the `Session`. So the injected content (e.g. the todo reminder) neither accumulates as turns grow nor changes the cached system-prompt prefix. Injectors are fail-open: a raising injector is logged and skipped, never aborting the run. The todo plugin (`plugins/todos/`) is the first consumer; the same seam is the primitive for ephemeral message insertion generally.

## Handoff mechanism

Handoffs use a **sentinel pattern** across three modules. When a handoff tool is invoked (`transfer_to_<name>`):

1. `handoff.py:build_handoff_tool()` — the tool's invoke returns a `_HandoffSignal(handoff=...)` dataclass (carrying the per-call `reason`) instead of a normal result.
2. `runtime/tool_calls.py:ToolCallProcessor.execute()` — detects `_HandoffSignal` via `isinstance()`, sets `state.pending_handoff`, and writes a text result to the transcript. Handoff tools always execute as **barriers** (never concurrently with other calls of the turn), which is what keeps "the first handoff of a turn wins" race-free under parallel tool execution.
3. `runtime/loop.py:RunLoop._apply_handoff()` — after the tool calls are processed, the loop checks `state.pending_handoff`; if set, it resolves a fresh `ActiveAgent` for the target via `_resolve_active()` (its own providers, tools, structured output, workspace, and plugin contributions) and swaps it in atomically with `RunState.activate()`, then rewrites the leading system message via `_reset_transcript_for_handoff()`.

This keeps the runner's main loop simple: handoff is just another tool result, flagged with a sentinel type. A handoff swaps only the leading system message for the target agent's and carries the conversation body across intact — the new agent sees the full prior context, tool calls included (providers replay calls for tools the new agent lacks fine, as long as each call keeps its paired result). The run-level `extra_instructions` addendum is re-applied to every agent reached by a handoff.

## Session vs Checkpointer

Two persistence concepts that serve different purposes. The model: **`session` = the log of completed runs; the checkpoint = the log of the in-flight run; the full transcript = `session.load() + snapshot.entries`.** Both stores are append-only and symmetric.

- **`Session`** (`session.py`) — the conversation transcript keyed by `session_id`, for multi-turn chat. **Append-only** (`load` / `append` / `clear`, no `replace`): the runner loads history at the start and, when a run finishes, `append`s that run's **own** entries as one segment keyed by `run_id` (generated when absent — most session-only runs have no checkpoint `run_id`). `SQLiteSession` stores one row per run in `session_runs` with `UNIQUE(session_id, run_id)`, so append is an idempotent `INSERT OR IGNORE` (a re-issued run never duplicates) and an old row is never rewritten; `load` concatenates in insertion order. Prior history is immutable — that's what lets resume safely reload it. Context compaction never writes to the Session.
- **`Checkpointer`** (`checkpointer.py`) — the in-flight run keyed by `run_id` (its **sole, global** key — not scoped by `session_id`, since a checkpointed run need not belong to a Session, so callers sharing one checkpointer must keep `run_id` unique across sessions). **Append-only** and symmetric with the Session: `append(run_id, entries, head)` adds a batch of entries (those since the last append) and overwrites a small mutable `RunHead` (usage + turns + status + `agent_name` + last-input-tokens + context-policy-state). `RunSnapshot = run_id + entries + head`, where `entries` is the run's **own** entries (not the prior history). `SQLiteCheckpointer` keeps one row per non-empty append (`snapshot_turns`) + one head row (`snapshot_heads`); the loop appends after the model output and after each tool call via `CheckpointWriter.save_running()`. On success the run's entries are appended to the Session **after** the checkpoint is finalized (`_stream_inner`), so a crash between the two can't leave the run both persisted *and* resumable (which would double-count on resume). `agent_name` records the *active* agent — after a handoff that is the target, not the entry agent. On resume `RunLoop` resolves that agent by name from the entry agent's handoff graph (`runtime/resume.py:resolve_resume_agent`), reloads history from the Session, and rebuilds the run as that agent, so multi-agent runs resume correctly.

Long-term cross-session **memory** is deliberately *not* a core runtime primitive — there is no `Memory` protocol baked into the loop or `Agent.memory` field. It ships instead as a first-class **plugin** (`Memory`, in `plugins/memory/`), built entirely on existing plugin seams (injected instructions, tools, and a `RunCompleted` hook that reads `session_id`/`run_id` and the active agent off the `RunContext` passed to every handler); see the **Memory** section in the README. Inside the package, `plugin.py` is pure policy over two narrow storage seams: `NotesStore` (hot tier, `load`/`save` a fact list) and `Index` (cold tier, `add`/`remove`/`search` over plain `Doc`s with upsert-by-id) — `index.py` ships the stdlib FTS5 `KeywordIndex` and the RRF-fusing `HybridIndex`, `vector.py` the `Embedder` seam and a stdlib `VectorIndex`. The same seams let you wire your own memory over a custom store.

## Context compaction

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
  call_id→preview records, running-summary text + coverage), and the pure
  function `render_view(transcript, state)` rebuilds the per-call view.
  Decisions are monotonic, so the rendered prompt prefix is byte-stable
  across turns — that is what keeps provider prompt caches warm. Never make a
  stage "undo" a decision.
- **Watermark hysteresis.** Nothing happens below `compact_at` (default 0.75
  of the usable window); a burst then shrinks the view to `compact_to`
  (default 0.50). Both accept a fraction (float) or absolute tokens (int).
  `TokenBudget` owns the math; `reserve_output_tokens` is subtracted first.
- **Cheap-first stages**: `OffloadToolResults` (replace huge results with a
  preview marker; archive the full output to the result store when one is set,
  else recall falls back to the transcript) → `ClearToolResults`
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
  structural `fingerprint` of the covered prefix detects a rewritten prefix
  (e.g. history trimmed before a new run reuses a carried summary) and resets
  the summary while keeping call_id-keyed decisions.
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
  capped via `Agent.max_tool_output_chars` (default 200K chars — a tripwire
  for runaway payloads; `None` opts out) or per-tool
  `@tool(max_output_chars=...)` — `ToolCallProcessor` truncates (head + tail
  + marker) before the entry is stored and drops the raw value. This is
  deliberately lossy; `recall_tool_result` sees the truncated version.

`safe_window()` in `transcript.py` is critical for any policy that drops middle entries — it ensures `ToolCallEntry`/`ToolResultEntry` pairs stay intact by walking the cut point backward to include orphaned call IDs.

## Testing internals

Tests use `ScriptedProvider` (public home: `lovia/testing.py`, so user eval
suites can use it too; `tests/scripted_provider.py` remains as a re-export
shim) — a deterministic, in-memory provider that replays pre-canned
`AssistantTurn` objects. No network calls. Build scripts with the `text()` and
`call()` helpers:

```python
from lovia.testing import ScriptedProvider, call, text

provider = ScriptedProvider([
    call("add", {"a": 2, "b": 3}, call_id="c1"),
    text("The answer is 5."),
])
```

The provider records every prompt it receives in `provider.calls` (as `list[list[Message]]`), so tests can assert on what the agent actually sent.

Context-system tests live under `tests/context/` (tokens, state, render,
stages, pipeline, recall, offload integration); eval-framework tests under
`tests/eval/` (checks against hand-built `RunResult`s, the engine and judge
against scripted agents). Live end-to-end tests against the real endpoint
configured in `.env` are in `tests/context/test_live_context.py`,
`tests/providers/test_live.py`, and `tests/eval/test_live.py`; run them with
`LOVIA_LIVE_TESTS=1 pytest -m live_provider` (the genuine context-overflow
probe additionally needs `LOVIA_LIVE_OVERFLOW_TESTS=1`).
