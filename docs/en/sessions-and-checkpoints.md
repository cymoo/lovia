# Sessions & checkpoints

Two different persistence problems, two stores. A **session** answers "what
has this conversation said so far?" — multi-turn memory across runs. A
**checkpoint** answers "how far did this run get?" — crash recovery and
idempotency within one run. Both are append-only: history is never
rewritten.

## Sessions

```python
from lovia import Runner, SQLiteSession

session = SQLiteSession("chat.db")

await Runner.run(agent, "My project is called Atlas.", session=session, session_id="u1")
result = await Runner.run(agent, "What is my project called?", session=session, session_id="u1")
# "Atlas" — the second run loaded the first run's transcript as history
```

You control `session_id` — key it by user, thread, ticket, whatever your
product calls a conversation. The runner loads prior history at run start
and, when the run **completes**, appends the run's own entries as one
*segment*. Two implementations ship: `SQLiteSession(path, *, wal=False)`
and `InMemorySession()`.

### The contract

The `Session` protocol is four methods — `segments(session_id)`,
`load(session_id)` (flat concatenation), `append(session_id, entries, *,
run_id=None, meta=None)`, `clear(session_id)` — with two properties doing
the heavy lifting:

- **Append-only.** There is deliberately no `replace`: a stored run is
  immutable. That's what lets a resumed run safely reload history, and what
  keeps run boundaries and per-run `meta` (e.g. carried
  [compaction state](context.md)) consistent.
- **Idempotent by `run_id`.** Appending again under an already-stored
  `run_id` is a no-op, so a replayed run never duplicates its entries.

Interrupted runs are *not* auto-appended — they live in their checkpoint.
Whether to record an abandoned partial as a finished segment is a **caller**
decision (the bundled web UI does this when a user stops a run); a finalized
partial must stay tool-consistent — `lovia.transcript.drop_dangling_tool_calls`
exists for exactly that.

### Maintenance

Long-lived sessions accumulate huge old tool outputs. The bundled stores
(not the protocol) provide one sanctioned carve-out from append-only:

```python
trimmed = await session.trim_tool_results("u1", keep_chars=400, keep_runs=1)
```

It truncates *stored* tool outputs older than the last `keep_runs` runs
(structure, order, and `call_id` pairing preserved; idempotent). Pair it
with a [`FileResultStore`](context.md#result-stores) on the compaction
policy *before* relying on it — archived outputs stay recoverable via
`recall_tool_result`; un-archived ones are truncated for good.

## Checkpoints

For runs that must survive crashes — or be safely re-issued — add a
checkpoint:

```python
from uuid import uuid4

from lovia import CheckpointOptions, Runner, SQLiteCheckpointer

cp = SQLiteCheckpointer("runs.db")

result = await Runner.run(
    agent,
    "Migrate the report format.",
    checkpoint=CheckpointOptions(cp, f"report-migration-{uuid4().hex}"),
)
```

The loop snapshots after the model turn and after **every tool result**, so
a crash loses at most the work in flight. A snapshot holds the run's own
entries plus a small head: active agent name, usage, turn count, status
(`running` / `interrupted` / `completed` / `failed`), and the context
policy's carried state. Your `context` object is *not* snapshotted — you
re-supply it when resuming.

### `run_id` is an idempotency key

`run_id` is the checkpoint's **sole, global** key — unlike a session it is
not scoped, so keep it unique per checkpointer (a UUID, a job id). What
happens when the id already exists is the `if_run_exists` policy:

| Policy | Existing run... | No stored run... |
| --- | --- | --- |
| `"resume"` (default) | continues it (replays it verbatim if already completed) | starts fresh |
| `"restart"` | discards it and starts fresh | starts fresh |
| `"fail"` | raises `UserError` | starts fresh |
| `"resume_only"` | continues it | raises `UserError` |

So a crashed worker just re-issues the same call: an interrupted run
resumes, a completed run replays its stored result without touching the
model, and either way the answer comes back. Two subtleties:

- **On resume, the new `input` is ignored** — the transcript already
  carries the original. `run_id` is a per-run idempotency key, not a
  conversation key; for conversational continuity use a session. To
  continue a known run *by id* with no new input:

  ```python
  Runner.run(agent, [], checkpoint=CheckpointOptions(cp, rid, if_run_exists="resume_only"))
  ```

- **A replay re-emits terminal events only.** Hooks and output guardrails
  ran on the original completion and are not re-run; usage folds into the
  caller; `finish_reason` is `None` (not persisted). Session persistence
  *is* re-applied — idempotently — which heals a crash that landed between
  checkpoint finalization and the session append.

`CheckpointOptions` also takes `delete_on_success=True` (drop the snapshot
once the run completes — for runs whose durable record is the session) and
`resume_from=` (rehydrate a `RunSnapshot` you obtained yourself).

### What resume actually does

Resuming rebuilds the run — system prompt re-rendered, session history
reloaded, the snapshot's entries appended — and then **drains pending tool
calls**: calls the crash left without results are re-executed (same turn
number) before the loop continues. Completed results are never re-run;
dangling calls are. Design your side-effecting tools for at-least-once, or
make them idempotent.

Resume works across [handoffs](multi-agent.md): the snapshot records the
*active* agent by name, and the runner re-resolves it from the entry
agent's handoff graph. Renaming or removing an agent breaks resume for its
in-flight runs (hard error); completed-run *replays* degrade gracefully to
the entry agent with a warning instead — finished work must not error on a
deploy.

## How the two stores relate

```
full transcript = session.load(session_id)   +   snapshot.entries
                  (all completed runs)            (the one in-flight run)
```

On success the order is fixed: checkpoint finalized **first**, then the
session append. A run can therefore never be both "persisted as done" and
"still resumable" — the crash window between the two heals on the next
replay, because the session append is idempotent.

Both SQLite stores accept `wal=True` (off by default): WAL journal mode
plus a busy timeout, for a database file shared with other writers —
several stores in one file, or a multi-process deployment.

## Sharp edges

- **Reusing a completed `run_id` silently drops your new input** — by
  design (replay). If you meant "next turn of the conversation", that's a
  session, not a checkpoint.
- **Tools re-execute on resume.** Only calls with stored results are safe;
  anything dangling runs again. Non-idempotent tools (charges, sends) need
  their own dedup keyed by `ctx.run_id` + call arguments.
- **`run_id` collisions across sessions corrupt nothing but confuse
  everything** — the checkpointer is global; two "runs" sharing an id are
  one run to it. Generate ids, don't compose them from user input.
- **`InMemory*` stores don't survive the process** — they exist for tests
  and ephemeral chat; pairing `InMemoryCheckpointer` with "crash recovery"
  is a category error.

## See also

- [Core concepts](concepts.md#session-vs-checkpoint) — the mental model
- [Context management](context.md) — carried state and result stores
- [Web UI & server](web.md) — how the bundled server wires sessions
- Examples: [`05_sessions.py`](../../examples/05_sessions.py),
  [`15_resume.py`](../../examples/15_resume.py)
