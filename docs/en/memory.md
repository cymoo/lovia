# Memory

Sessions preserve one conversation. The `Memory` plugin carries selected
information across conversations in two tiers:

- **Notes** (hot) — a small, char-budgeted block of durable facts,
  **always injected** into the system prompt (pinned per session, so the
  prompt cache survives). The agent writes them itself with
  `remember(fact)` / `forget(fact)`.
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
├── archive.db     # cold tier: keyword index of past conversations
├── vectors.db     # cold tier: vector arm (only with embedder=)
└── scopes/        # only with scope= — one directory per scope key
    └── lovia-3f9a1c2e/  # the key's last segment + a short hash of it
        ├── scope.json   # the original key
        ├── MEMORY.md    # the scope's Notes
        └── archive.db   # the scope's past conversations (and vectors.db)
```

`MEMORY.md` is directly editable. Writes are atomic, and non-bullet lines are
ignored on load.

> **Privacy.** The archive persists user and assistant message text to
> disk. Keep the memory directory under appropriate access control, and
> pass `index=None` to keep no searchable record of conversations (Notes
> only — `recall` disappears).

## How memories get written

**The agent is the only one who writes Notes by itself.** Nothing extracts
facts from a finished run: a note exists because the agent called `remember`
at the moment the user said something worth keeping — the one point where
the whole conversation is in view, so a standing rule can be told from a
one-off request.

What earns a note is spelled out in the tool's description — a closed list
of four kinds, each taken only from what the user said or confirmed:

- how the user wants the agent to work (a correction, a standing preference);
- who the user is (their role, lasting circumstances);
- where things are (servers, accounts, tools the user would otherwise have
  to point to again);
- decisions and constraints of the user's work that its files and history
  don't show.

Everything else has another home and stays out: what the project's files,
instruction files, or version history already record; progress and results
of the task at hand; the agent's own findings; guesses at the user's
interests or mood; one-off requests; general knowledge. When unsure, the
agent doesn't save — a missed fact only has to be said again, a wrong one
misleads every later session. An explicit "remember this" from the user is
always honored.

Notes stay small by construction rather than by cleanup:

- **A fact that supersedes a note replaces it** —
  `remember(fact, replaces=<the old note>)` swaps the two in one write, so a
  correction never sits next to what it corrected.
- **A full list refuses new notes.** Past `notes_budget` (default 4000
  chars per layer; the agent sees a meter), `remember` answers with the
  current notes and asks the agent to make room — replace one or `forget`
  one — before saving.
- New notes are date-stamped `[YYYY-MM]`, so the agent (and you) can see
  how old a note is.

The archive needs no judgment at all: at each run's end (`RunCompleted`)
the run is indexed as **one document** — its user and assistant turns in
order, a question next to its answer — with the run id as the document id,
so a resumed run upserts instead of duplicating. Tool calls and results
stay out, and a very long run keeps its head and its tail. No model call is
involved.

Your code can write too — the same verbs are public methods, no model in
the loop:

```python
mem = Memory("./memory")
await mem.remember("Prefers concise answers in Chinese.")
await mem.remember("Deploys with uv.", replaces="Deploys with pip.")
await mem.forget("old preference")
body = await mem.notes_body()          # editor read
await mem.replace_notes(edited_body)   # editor write (normalizes + dedups)
```

`GET` / `PUT /api/memory` use that last pair. Writes from code are not held
to `notes_budget`.

### Extracting facts yourself

Want facts pulled out of every finished run anyway — an onboarding flow, a
support desk that must capture a customer's details? That is a
`RunCompleted` hook and `mem.remember()`, with whatever extractor and policy
fit your traffic:

```python
from lovia import Agent, AgentHooks, Memory
from lovia.events import RunCompleted

mem = Memory("./memory")
hooks = AgentHooks()

@hooks.on(RunCompleted)
async def extract(ev: RunCompleted, ctx) -> None:
    for fact in await my_extractor(ev.result.entries):   # your model call
        await mem.remember(fact)

agent = Agent(name="assistant", model="<model>", plugins=[mem], hooks=hooks)
```

## Notes and the prompt cache

Notes live in the system prompt, and providers cache the prompt prefix —
system prompt first, then the whole conversation history. Changing Notes
mid-conversation would make the next run re-pay for all of that history.
So each session stays **pinned** to the Notes it started with: facts
remembered meanwhile hit `MEMORY.md` at once and are already visible in the
conversation itself, and the system prompt stays byte-identical.

Once a session sits idle for `notes_refresh_after` seconds (default 300 —
Anthropic's default cache lifetime), its next run re-reads Notes; by then
the provider cache has expired anyway, so the refresh is free. Raise it to
match a longer cache lifetime, or set `0` to re-read Notes on every run.
Runs without a `session_id` always read live Notes, and
`replace_notes()` or `clear_scope()` — a deliberate edit — unpins every
session. Snapshots live in process memory: a restart simply re-reads.

## Scopes

One Notes file for everything works for an assistant in one place. An agent
that works across projects needs two kinds of memory: what is true of the
*user* (wherever they work), and what is true of *this project* (its
decisions, its past conversations). `scope=` maps each run to a key, and
each key gets its own Notes and archive next to the user's:

```python
Memory(
    "~/.lovia/memory",
    scope=lambda ctx: ctx.deps.project_path,  # (RunContext) -> str | None
    scope_name="project",                     # what the prompts call it
)
```

The function runs once when a run starts; the whole run — tools, prompt,
archive — then stays in that scope. Returning `None` puts the run in no
scope: it behaves exactly like a `Memory` without `scope=`. In a scope:

| | Unscoped run | Run in a scope |
| --- | --- | --- |
| Prompt | `NOTES` | `USER NOTES` + `PROJECT NOTES`, each with its own meter and full `notes_budget` |
| `remember` | `remember(fact)` | `remember(fact, about)`, `about` is `"user"` or `scope_name` |
| `replaces` / `forget` | the user's Notes | both layers (the closest match wins) |
| `recall` | the top-level archive | only this scope's archive |
| Run end | the run → the top-level archive | the run → this scope's archive |

The model decides which layer a fact belongs to, with one rule: about the
person (a preference, a standing rule, their lasting circumstances) → user;
about this project or this work → the scope; unsure → the scope, because a
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
The project scope in Settings → Memory shows only for a Memory scoped by it:

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
| `expand_query` | `"auto"` | LLM query expansion; auto = only for the lexical-only default index |
| `summarize_recall` | `True` | `recall` returns a model-written summary of the hits |
| `recall_k` | `5` | hits retrieved per recall |
| `notes_budget` | `4000` | char budget for a layer's Notes — the prompt meter, and where `remember` starts refusing |
| `notes_refresh_after` | `300` | seconds a session keeps its Notes snapshot after its last run; `0` re-reads every run |
| `model` | host agent's model | model for `recall`'s side-queries |

Memory calls a model only when the agent asks to `recall`. Those
side-queries (expansion, summarization) dogfood `Runner.run` with a
tool-less, plugin-less sub-agent at temperature 0 — so they reuse your
provider chain and **cannot recurse** (the sub-agent has no Memory plugin).

## Sharp edges

- **Custom backends are shared by every run** — possibly concurrently.
  They must be safe for concurrent use, and the plugin never closes them;
  their lifecycle belongs to whoever created them. (Notes read-modify-write
  is serialized by an internal lock; SQLite stores serialize internally.)
- **What gets remembered is the model's call.** A weaker model may miss a
  fact worth keeping or save one that isn't; the admission eval
  (`tests/plugins/test_memory_admission_eval.py`) is how to check a model
  you plan to run. Notes are plain text in `MEMORY.md` — read them, edit
  them.
- **One `recall` may add two side calls.** The default lexical index expands
  the query, and the retrieved hits are summarized. Set `expand_query=False`
  and `summarize_recall=False` respectively to disable them. Nothing else in
  Memory calls a model.
- **Indexing runs before the run returns.** With `embedder=`, that is one
  embedding request per run; a failure is logged and never fails the run.
- **`recall` is only as good as what was archived.** `index=None` earlier
  means those conversations are simply not findable later; the archive
  starts when you turn it on. Runs archived by versions before 0.9.311 stay
  searchable in their older per-message form.
- **A scope is its key.** Rename or move a project directory and a path key
  changes, so the old memory stays under the old key. A scope's history
  never shows up in another scope's `recall`, nor in unscoped runs.

## See also

- [Plugins](plugins.md) — Memory is the flagship cross-run-state plugin
- [Sessions & checkpoints](sessions-and-checkpoints.md) — within-conversation
  persistence, and where the transcripts come from
- [Web UI](web-ui.md) — the bundled Memory editor
- Example: [`23_memory.py`](../../examples/23_memory.py)
