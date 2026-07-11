# Memory

Sessions give an agent history *within* a conversation; nothing carries
across conversations — the user re-explains their preferences every chat.
The `Memory` plugin adds long-term memory built from **two tiers and three
verbs** the model already understands:

- **Notes** (hot) — a small, char-budgeted block of durable facts,
  **always injected** into the system prompt. Curated with
  `remember(fact)` / `forget(fact)`.
- **Archive** (cold) — a full-text-searchable store of past conversations,
  pulled in only on demand with `recall(query)`.

```python
from lovia import Agent, Memory

agent = Agent(
    name="assistant",
    model="glm-5.2",
    plugins=[Memory("./.lovia/memory")],
)
```

That's the whole zero-config setup: stdlib SQLite full-text search, no
services, no embeddings — and it recalls surprisingly well, because the LLM
covers the lexical gaps (below).

## What lands on disk

```
.lovia/memory/
├── MEMORY.md      # hot tier: one `- fact` per line — human-editable
├── archive.db     # cold tier: keyword index of past conversations
└── vectors.db     # cold tier: vector arm (only with embedder=)
```

`MEMORY.md` is deliberately plain markdown: open it in an editor, delete a
line, done. Writes are atomic, and non-bullet lines are ignored on load.

> **Privacy.** The archive persists user and assistant message text to
> disk. Keep the memory directory under appropriate access control, and
> pass `index=None` to keep no searchable record of conversations (Notes
> only — `recall` disappears).

## How memories get written

Three paths, from automatic to manual:

1. **Auto-curation** (`auto_curate=True`, default). At each run's end
   (`RunCompleted`), one digest call over the complete transcript promotes
   the few durable facts into Notes and writes a self-contained episode
   summary into the archive — where it searches far better than raw chat
   fragments. Raw user/assistant messages are indexed too; document ids are
   deterministic (`run_id:seq`), so a replayed run upserts instead of
   duplicating.
2. **The model, mid-run** — `remember` / `forget` tools, guided by injected
   instructions ("save durable facts proactively").
3. **Your code** — the same verbs are public methods, no model in the loop:

   ```python
   mem = Memory("./memory")
   await mem.remember("Prefers concise answers in Chinese.")
   await mem.forget("old preference")
   body = await mem.notes_body()          # editor read
   await mem.replace_notes(edited_body)   # editor write (normalizes + dedups)
   ```

   The web UI's sidebar Memory editor (`GET`/`PUT /api/memory`) is built on
   that last pair.

Notes stay within `notes_budget` (default 5000 chars — a meter is shown to
the model): when the budget overflows after a digest, one consolidation
call merges and rewrites the list to fit.

By default curation runs **inline** — when `Runner.run` returns, memory is
settled. A long-lived host passes `curate_in_background=True` so the run's
final event isn't held back by curation's model calls, and awaits
`mem.drain()` on shutdown to settle anything in flight (the bundled web
server does exactly this, with a 15s bound).

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
`OPENAI_EMBEDDING_BASE_URL` / `OPENAI_EMBEDDING_API_KEY` first, falling
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
| `auto_curate` | `True` | run-end digest: facts → Notes, episode summary → archive; consolidates over-budget Notes |
| `curate_in_background` | `False` | don't hold the run's completion for curation; pair with `drain()` |
| `expand_query` | `"auto"` | LLM query expansion; auto = only for the lexical-only default index |
| `summarize_recall` | `True` | `recall` returns a model-written summary of the hits |
| `recall_k` | `5` | hits retrieved per recall |
| `notes_budget` | `5000` | char budget for Notes — the prompt meter and consolidation trigger |
| `model` | host agent's model | model for the curation/recall side-queries |

The side-queries (digest, consolidation, expansion, summarization) dogfood
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
- **Curation costs a model call per run** (two when Notes overflow). On
  high-volume, low-value traffic, set `auto_curate=False` and rely on the
  `remember` tool, or point `model=` at a cheaper model.
- **Background curation is best-effort.** A process that exits without
  `drain()` can lose the last run's curation — acceptable by design (the
  transcript is still in the session), but surprising if you expected
  durability.
- **`recall` is only as good as what was archived.** `index=None` earlier
  means those conversations are simply not findable later; the archive
  starts when you turn it on.

## See also

- [Plugins](plugins.md) — Memory is the flagship cross-run-state plugin
- [Sessions & checkpoints](sessions-and-checkpoints.md) — within-conversation
  persistence, and where the transcripts come from
- [Web UI & server](web.md) — the bundled Memory editor
- Example: [`23_memory.py`](../../examples/23_memory.py)
