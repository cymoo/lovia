# Memory

Sessions preserve one conversation. The `Memory` plugin carries selected
information across conversations in two tiers:

- **Notes** (hot) — a small, char-budgeted block of durable facts,
  **always injected** into the system prompt (pinned per session, so the
  prompt cache survives). Curated with `remember(fact)` / `forget(fact)`.
- **Archive** (cold) — a full-text-searchable store of past conversations,
  pulled in only on demand with `recall(query)`.

If information only needs to survive within one conversation, a Session is
enough. Enable Memory for cross-conversation reuse. Use
`Memory(..., index=None)` when you want Notes without a searchable archive.

```python
from lovia import Agent, Memory

agent = Agent(
    name="assistant",
    model="<model>",
    plugins=[Memory("./.lovia/memory")],
)
```

That's the whole zero-config setup: stdlib SQLite full-text search, no
services, no embeddings — and it recalls surprisingly well, because the LLM
covers the lexical gaps (below).

## What lands on disk

```
.lovia/memory/
├── MEMORY.md      # hot tier: one `- [YYYY-MM] fact` per line — human-editable
├── MEMORY.md.bak  # the previous Notes, written before each dream (one-level undo)
├── .dreamed       # mtime marker: when the last dream ran
├── archive.db     # cold tier: keyword index of past conversations
├── vectors.db     # cold tier: vector arm (only with embedder=)
└── scopes/        # only with scope= — one directory per scope key
    └── lovia-3f9a1c2e/  # the key's last segment + a short hash of it
        ├── scope.json   # the original key
        ├── MEMORY.md    # the scope's Notes (plus its own .bak, .dreamed)
        └── archive.db   # the scope's past conversations (and vectors.db)
```

`MEMORY.md` is directly editable. Writes are atomic, and non-bullet lines are
ignored on load.

> **Privacy.** The archive persists user and assistant message text to
> disk. Keep the memory directory under appropriate access control, and
> pass `index=None` to keep no searchable record of conversations (Notes
> only — `recall` disappears).

## How memories get written

Three paths, from automatic to manual:

1. **Auto-curation** (`auto_curate=True`, default). At each run's end
   (`RunCompleted`), one digest call over the complete transcript promotes
   the few durable facts into Notes — date-stamped `[YYYY-MM]`, admission
   is deliberately strict (no re-derivable state, no one-off task details,
   most runs yield nothing) and takes facts only from what the user said or
   explicitly confirmed — the assistant's own explanations and conclusions,
   including whatever it drew from tool output, can be wrong and never
   become notes on their own — retires existing notes the conversation
   disproved, and writes a self-contained episode summary into the archive,
   where it searches far better than raw chat fragments. Raw user/assistant
   messages are indexed too; document ids are deterministic (`run_id:seq`),
   so a replayed run upserts instead of duplicating.
2. **The model, mid-run** — `remember` / `forget` tools, reserved for
   explicit asks and corrections ("remember that I…"); everything else
   rides the run-end digest. Detail lives in the archive, not in Notes.
3. **Your code** — the same verbs are public methods, no model in the loop:

   ```python
   mem = Memory("./memory")
   await mem.remember("Prefers concise answers in Chinese.")
   await mem.forget("old preference")
   body = await mem.notes_body()          # editor read
   await mem.replace_notes(edited_body)   # editor write (normalizes + dedups)
   ```

   `GET` / `PUT /api/memory` use that same pair.

Setting `auto_curate=False` disables the run-end digest and episode summary,
but it does not stop raw user and assistant messages from entering the Archive.
Use `index=None` when no searchable conversation record should be stored.

## Notes and the prompt cache

Notes live in the system prompt, and providers cache the prompt prefix —
system prompt first, then the whole conversation history. Changing Notes
mid-conversation would make the next run re-pay for all of that history.
So each session stays **pinned** to the Notes it started with: facts
remembered (or curated) meanwhile hit `MEMORY.md` at once and are already
visible in the conversation itself, and the system prompt stays
byte-identical.

Once a session sits idle for `notes_refresh_after` seconds (default 300 —
Anthropic's default cache lifetime), its next run re-reads Notes; by then
the provider cache has expired anyway, so the refresh is free. Raise it to
match a longer cache lifetime, or set `0` to re-read Notes on every run.
Runs without a `session_id` always read live Notes, and
`replace_notes()` or `clear_scope()` — a deliberate edit — unpins every
session. Snapshots live in process memory: a restart simply re-reads.

## Dreaming

Any always-append memory bloats: near-duplicates pile up, corrections
coexist with what they corrected, one-off details fossilize. So the plugin
periodically **dreams** — one model call rewrites the whole list: merging
near-duplicates into one well-phrased note, resolving contradictions
(newer wins, by the `[YYYY-MM]` stamps), dropping event-like or expired
entries — while never inventing a fact and never dropping your explicit
rules and preferences.

Three triggers:

- **Cadence** — every `dream_every_days` (default 3), checked at run end
  and skipped while Notes haven't changed since the last dream. The state
  is just the mtime of the `.dreamed` marker.
- **Budget overflow** — Notes past `notes_budget` (default 8000 chars; the
  model sees a meter) dream immediately.
- **On demand** — `await mem.dream()` returns `(before, after)` note
  counts and can tidy a bloated legacy file.

Before each rewrite the previous body lands in `MEMORY.md.bak` — a
one-level undo. Deeper history is what git and the archive are for: the
archive keeps the conversations every note came from.

By default curation runs **inline** — when `Runner.run` returns, memory is
settled. A long-lived host passes `curate_in_background=True` so the run's
final event isn't held back by curation's model calls, and awaits
`mem.drain()` on shutdown to settle anything in flight (the bundled web
server does exactly this, with a 15s bound).

## Scopes

One Notes file for everything works for an assistant in one place. An agent
that works across projects needs two kinds of memory: what is true of the
*user* (wherever they work), and what is true of *this project* (its code,
its conventions, its past conversations). `scope=` maps each run to a key,
and each key gets its own Notes and archive next to the user's:

```python
Memory(
    "~/.lovia/memory",
    scope=lambda ctx: ctx.deps.project_path,  # (RunContext) -> str | None
    scope_name="project",                     # what the prompts call it
)
```

The function runs once when a run starts; the whole run — tools, prompt,
curation — then stays in that scope. Returning `None` puts the run in no
scope: it behaves exactly like a `Memory` without `scope=`. In a scope:

| | Unscoped run | Run in a scope |
| --- | --- | --- |
| Prompt | `NOTES` | `USER NOTES` + `PROJECT NOTES`, each with its own meter and full `notes_budget` |
| `remember` | `remember(fact)` | `remember(fact, about)`, `about` is `"user"` or `scope_name` |
| `forget` | the user's Notes | both layers (the closest match wins) |
| `recall` | the top-level archive | only this scope's archive |
| Run-end digest | facts → user Notes, summary → top-level archive | facts about the user → user Notes, facts about the project → its Notes, summary → its archive |
| Dream | the user's Notes | each layer on its own `.dreamed` cadence |

The model decides which layer a fact belongs to, with one rule: about the
person (a preference, a standing rule, their lasting circumstances) → user;
about this codebase or this work → the scope; unsure → the scope, because a
project fact made global leaks into every other project, while a
preference kept in one project only has to be said again elsewhere.

Every public method that reads or writes a layer takes `scope=`: `None`
(default) is the user's layer, a key is that scope's. `clear_scope(key)`
deletes a scope's Notes *and* archive — the archive is otherwise out of
reach:

```python
await mem.remember("Deploys go through `make ship`.", scope="/srv/app")
body = await mem.notes_body(scope="/srv/app")
await mem.replace_notes(edited, scope="/srv/app")
await mem.dream(scope="/srv/app")
await mem.clear_scope("/srv/app")          # irreversible
```

Any string is a valid key (`/srv/app`, `ssh://box/x`): the directory name is
the key's last segment plus a short hash, and `scope.json` records the key
itself. Scopes live only in the default stores, so `scope=` can't be
combined with `notes=` or a custom `index=`; `index=None` and `embedder=`
apply to every layer.

Served with `lovia.web`, `scope=chat_project` splits memory by the chat's
directory — what `lovia web` does. Its key is the chat's workspace (a
resolved path, or `ssh://…` / `docker://…`), and a chat in the
`WorkspaceSwitch(general=)` catch-all, or without a workspace, has no scope.
The Memory dialog's per-project tab shows only for a Memory scoped by it:

```python
from lovia.web import chat_project

Memory("~/.lovia/memory", scope=chat_project, scope_name="project")
```

## Recall quality, one argument at a time

```python
Memory("./memory")                             # stdlib keyword search (FTS5 bm25)
Memory("./memory", embedder=OpenAIEmbedder())  # + semantic arm → hybrid recall
Memory("./memory", index=my_index)             # bring your own retrieval engine
```

**Zero-config** is SQLite FTS5 — bm25 over a CJK-aware bigram index — and
the LLM covers what keywords miss: `recall` queries are expanded with
synonyms and translations before searching (`expand_query="auto"` turns
this on exactly when the index is lexical-only), and hits come back as a
model-written summary rather than raw excerpts (`summarize_recall=True`).
Both LLM assists fail open: an expansion or summary error degrades to the
raw query / raw hits.

**`embedder=`** upgrades the default index to a keyword | vector hybrid
fused by Reciprocal Rank Fusion — semantic and cross-lingual recall with no
new services (vectors live in SQLite). `OpenAIEmbedder` speaks any
OpenAI-compatible `/embeddings` endpoint:

```python
OpenAIEmbedder(model="text-embedding-3-small", dimensions=None, batch_size=32)
```

Chat and embeddings often live on different hosts, so the embedder reads
`LOVIA_EMBEDDING_BASE_URL` / `LOVIA_EMBEDDING_API_KEY` first, falling
back to the chat endpoint's `OPENAI_BASE_URL` / `OPENAI_API_KEY`. Changing
embedders is safe: vectors are a recall cache keyed by embedder id — a
mismatch wipes and re-accumulates rather than mixing spaces.

**`index=`** replaces retrieval outright. An `Index` is three methods over
plain docs — `add` / `remove` / `search`, upsert by `Doc.id` — implement it
over Elasticsearch, pgvector, whatever:

```python
class Index(Protocol):
    async def add(self, docs: list[Doc]) -> None: ...
    async def remove(self, ids: list[str]) -> None: ...
    async def search(self, query: str, k: int = 5) -> list[Hit]: ...
```

Compose arms with `|` — `KeywordIndex(...) | VectorIndex(...) | my_arm` is
one RRF-fused `HybridIndex` whose reads fail open (a broken arm is skipped)
and whose writes go to every arm; any index gains the `|` operator by
mixing in `Fusable`. (`embedder=` and `index=` are mutually exclusive — the
embedder is sugar for building the hybrid.)

Likewise the hot tier: `NotesStore` is two methods (`load`/`save` a fact
list); all normalization, dedup, and budget policy stays in the plugin, so
a Redis- or DB-backed store is a dozen lines (`FileNotesStore` — the
`MEMORY.md` writer — is the reference one).

## Configuration reference

| Field | Default | Effect |
| --- | --- | --- |
| `root` | `./.lovia/memory` | where the default stores live (ignored for a tier you pass explicitly) |
| `notes` | `None` → `MEMORY.md` file store | hot-tier backend |
| `index` | default keyword index | cold-tier backend; `None` disables the tier and the `recall` tool |
| `embedder` | `None` | adds the vector arm to the default index |
| `scope` | `None` | `(RunContext) -> str \| None`: splits memory by a per-run key (see [Scopes](#scopes)) |
| `scope_name` | `"project"` | what the prompts call a scope, and `about`'s value for it |
| `auto_curate` | `True` | run-end digest: facts → Notes (disproved notes retired), episode summary → archive |
| `curate_in_background` | `False` | don't hold the run's completion for curation; pair with `drain()` |
| `expand_query` | `"auto"` | LLM query expansion; auto = only for the lexical-only default index |
| `summarize_recall` | `True` | `recall` returns a model-written summary of the hits |
| `recall_k` | `5` | hits retrieved per recall |
| `notes_budget` | `8000` | char budget for Notes — the prompt meter and the dream's overflow trigger |
| `dream_every_days` | `3` | cadence for the periodic dream; `None` keeps only the overflow trigger |
| `notes_refresh_after` | `300` | seconds a session keeps its Notes snapshot after its last run; `0` re-reads every run |
| `model` | host agent's model | model for the curation/recall side-queries |

The side-queries (digest, dream, expansion, summarization) dogfood
`Runner.run` with a tool-less, plugin-less sub-agent at temperature 0 — so
they reuse your provider chain and **cannot recurse** (the sub-agent has no
Memory plugin). Because lovia's transcript is durable and
[compaction is view-only](context.md), the digest runs once over the
*complete* transcript: it is curation, not rescue.

## Sharp edges

- **Custom backends are shared by every run** — possibly concurrently.
  They must be safe for concurrent use, and the plugin never closes them;
  their lifecycle belongs to whoever created them. (Notes read-modify-write
  is serialized by an internal lock; SQLite stores serialize internally.)
- **Curation costs a model call per run** (two when a dream is due). On
  high-volume, low-value traffic, set `auto_curate=False` and rely on the
  `remember` tool, or point `model=` at a cheaper model.
- **One `recall` may add two side calls.** The default lexical index expands
  the query, and the retrieved hits are summarized. Set `expand_query=False`
  and `summarize_recall=False` respectively to disable them.
- **Dream state lives under `root`** — the `.dreamed` cadence marker and
  the `MEMORY.md.bak` backup are plain files there, even when `notes=` is
  a custom store (the backup then holds an export of the store's facts).
- **Background curation is best-effort.** A process that exits without
  `drain()` can lose the last run's curation — acceptable by design (the
  transcript is still in the session), but surprising if you expected
  durability.
- **`recall` is only as good as what was archived.** `index=None` earlier
  means those conversations are simply not findable later; the archive
  starts when you turn it on.
- **A scope is its key.** Rename or move a project directory and a path key
  changes, so the old memory stays under the old key. A scope's history
  never shows up in another scope's `recall`, nor in unscoped runs.

## See also

- [Plugins](plugins.md) — Memory is the flagship cross-run-state plugin
- [Sessions & checkpoints](sessions-and-checkpoints.md) — within-conversation
  persistence, and where the transcripts come from
- [Web UI](web-ui.md) — the bundled Memory editor
- Example: [`23_memory.py`](../../examples/23_memory.py)
