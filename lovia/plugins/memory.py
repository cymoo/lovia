"""Memory plugin: tiered, cross-session memory for an agent.

``Memory`` gives an agent a long-term memory that survives across runs and
sessions, built from two tiers and three verbs the model already understands:

* **Notes** (the *hot* tier) — a tiny, char-budgeted block that is **always
  injected** into the system prompt. It holds the user's stable preferences,
  durable facts, and important context. The model curates it with
  ``remember(fact)`` / ``forget(fact)``, and (when ``auto_extract``) the plugin
  promotes durable facts into it automatically at the end of each run.
* **Archive** (the *cold* tier) — a full-text-searchable store of past
  conversations, pulled in only on demand via ``recall(query)``.

So the surface is intuitive: **Memory** + **remember / recall / forget**, over
**Notes** (hot) and **Archive** (cold). This synthesizes Claude Code's
multi-file memory and Hermes' tiered memory — the realization being that *the
unit that matters is the tier, not the file*: keep the hot tier as one small
always-present blob and put the cold tier in a real search index.

This fits lovia especially well because the transcript is durable and
compaction is **view-only** (``ContextCompacted.entries_before`` is the full
transcript and the ``Session`` persists it). So nothing is ever lost from the
record, and the only end-of-run work is *curation*: ingesting the run into the
archive and promoting durable facts into Notes. That is also why this plugin
hooks only :class:`~lovia.events.RunCompleted` and not ``ContextCompacted`` — at
run end ``result.entries`` already holds this run's complete entries (compaction
is view-only), so a separate pre-compaction flush would just re-extract the same
facts at extra cost. (``result.entries`` is this run's own messages, not the
whole session, so the archive appends per run rather than re-ingesting history.)

Defaults are filesystem markdown (Notes) + SQLite FTS5 (Archive), each behind a
small ``Protocol`` so the backends are swappable::

    from lovia import Agent, Memory

    agent: Agent[Any] = Agent(name="assistant", plugins=[Memory("./.lovia/memory")])
    # -> FileNotesStore("./.lovia/memory/MEMORY.md")
    #  + SQLiteArchiveStore("./.lovia/memory/archive.db")

Backends are long-lived and shared by every run (held on the plugin, never
rebuilt per run, never closed by the plugin); :meth:`Memory.setup` only
assembles the per-run tools, instructions, and the ``RunCompleted`` hook.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Protocol,
    cast,
    runtime_checkable,
)

from pydantic import BaseModel, Field

from ..events import RunCompleted
from ..hooks import AgentHooks
from ..parts import text_of
from ..run_context import RunContext
from ..stores._sqlite import SQLiteStore
from ..tools import Tool, tool
from ..transcript import TranscriptEntry, entries_to_messages
from .base import PluginInstance

if TYPE_CHECKING:
    from ..providers import Provider

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = "./.lovia/memory"
_NOTES_FILENAME = "MEMORY.md"
_ARCHIVE_FILENAME = "archive.db"
_DEFAULT_MAX_CHARS = 2000

# Sentinel for ``Memory.archive``: distinguishes "build the default archive"
# (the field was left untouched) from an explicit ``archive=None`` ("no cold
# tier"). Without it, ``None`` would be ambiguous and could not disable the
# archive when ``notes`` is a path. Typed ``Any`` so the public field annotation
# stays ``ArchiveStore | None``.
_DEFAULT_ARCHIVE: Any = object()


# ---------------------------------------------------------------------------
# Notes body helpers — the canonical hot-tier format is one fact per line,
# rendered as a ``- fact`` bullet list (markdown-native, model-friendly, and
# trivial to add/remove/dedup).
# ---------------------------------------------------------------------------


def _normalize_fact(fact: str) -> str:
    """Collapse a fact to a single trimmed line (notes are one fact per line)."""
    return " ".join(fact.split())


def _parse_facts(body: str) -> list[str]:
    """Parse a stored notes body (``- fact`` per line) into its facts."""
    facts: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            fact = stripped[2:].strip()
            if fact:
                facts.append(fact)
    return facts


def _format_facts(facts: list[str]) -> str:
    """Render facts back to the canonical notes body."""
    return "\n".join(f"- {f}" for f in facts)


def _drop_fact(facts: list[str], target: str) -> list[str]:
    """Remove the best match for ``target`` (exact → case-insensitive → substring)."""
    if target in facts:
        out = list(facts)
        out.remove(target)
        return out
    low = target.lower()
    for i, f in enumerate(facts):
        if f.lower() == low:
            return facts[:i] + facts[i + 1 :]
    for i, f in enumerate(facts):
        fl = f.lower()
        if low in fl or fl in low:
            return facts[:i] + facts[i + 1 :]
    return list(facts)


def _meter(used: int, total: int) -> str:
    pct = round(100 * used / total) if total else 0
    return f"[{pct}% — {used:,}/{total:,} chars]"


# ---------------------------------------------------------------------------
# Hot tier: Notes
# ---------------------------------------------------------------------------


@runtime_checkable
class NotesStore(Protocol):
    """The hot tier: a tiny, always-injected, self-curating note block.

    The body convention is one fact per line (``raw`` returns it, ``replace``
    sets it); :meth:`render` wraps it with a capacity meter for injection.
    """

    async def render(self) -> str:
        """Return the block to inject into the system prompt (with a meter)."""
        ...

    async def raw(self) -> str:
        """Return the stored notes body."""
        ...

    async def add(self, fact: str) -> None:
        """Add a fact (idempotent — duplicates are ignored)."""
        ...

    async def remove(self, fact: str) -> None:
        """Remove a fact (best-effort match)."""
        ...

    async def replace(self, content: str) -> None:
        """Replace the whole body (used by consolidation)."""
        ...


class FileNotesStore:
    """Default :class:`NotesStore`: a single markdown file, one fact per line.

    Facts are stored as a ``- fact`` bullet list and char-budgeted
    (model-agnostic, Hermes-style). Writes are serialized with an
    ``asyncio.Lock`` and made atomic via a temp-file rename, so concurrent runs
    never corrupt the file and readers never see a half-written body.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] = f"{_DEFAULT_ROOT}/{_NOTES_FILENAME}",
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self._path = Path(path)
        self.max_chars = max_chars
        self._lock = asyncio.Lock()

    def _read(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _write(self, body: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, self._path)

    async def raw(self) -> str:
        return (await asyncio.to_thread(self._read)).strip()

    async def render(self) -> str:
        body = await self.raw()
        meter = _meter(len(body), self.max_chars)
        if not body:
            return f"NOTES {meter}\n(empty — use `remember` to save durable facts)"
        return f"NOTES {meter}\n{body}"

    async def add(self, fact: str) -> None:
        norm = _normalize_fact(fact)
        if not norm:
            return
        async with self._lock:
            facts = _parse_facts(await asyncio.to_thread(self._read))
            if any(norm.lower() == f.lower() for f in facts):
                return
            facts.append(norm)
            await asyncio.to_thread(self._write, _format_facts(facts))

    async def remove(self, fact: str) -> None:
        norm = _normalize_fact(fact)
        if not norm:
            return
        async with self._lock:
            facts = _parse_facts(await asyncio.to_thread(self._read))
            kept = _drop_fact(facts, norm)
            if len(kept) != len(facts):
                await asyncio.to_thread(self._write, _format_facts(kept))

    async def replace(self, content: str) -> None:
        async with self._lock:
            # Re-normalize through parse/format so a raw payload still lands as
            # the canonical one-fact-per-line body; fall back to the trimmed
            # text when it has no bullets.
            facts = _parse_facts(content)
            body = _format_facts(facts) if facts else content.strip()
            await asyncio.to_thread(self._write, body)


# ---------------------------------------------------------------------------
# Cold tier: Archive
# ---------------------------------------------------------------------------


@dataclass
class ArchiveHit:
    """One search result from the cold :class:`ArchiveStore` tier."""

    session_id: str
    when: float
    text: str
    score: float


@runtime_checkable
class ArchiveStore(Protocol):
    """The cold tier: a searchable archive of past sessions."""

    async def ingest(
        self,
        session_id: str | None,
        entries: list[TranscriptEntry],
        *,
        run_id: str | None = None,
    ) -> None:
        """Append one run's own messages, replacing any prior copy of ``run_id``.

        ``entries`` is the run's **own** transcript (not the whole session), so
        each completed run adds only its new messages; re-ingesting the same
        ``run_id`` replaces them (idempotent on a resumed completion). Mirrors
        :meth:`~lovia.session.Session.append`.
        """
        ...

    async def search(self, query: str, k: int = 5) -> list[ArchiveHit]:
        """Return up to ``k`` archived messages relevant to ``query``."""
        ...


def _archive_docs(entries: list[TranscriptEntry]) -> list[str]:
    """Pull the user/assistant message texts worth archiving from a transcript."""
    docs: list[str] = []
    for m in entries_to_messages(entries):
        if m.role not in ("user", "assistant"):
            continue
        text = text_of(m.content).strip()
        if text:
            docs.append(text)
    return docs


# CJK-aware term extraction. SQLite's default ``unicode61`` FTS tokenizer (and a
# plain ``LIKE``) can't segment scripts written without spaces between words: a
# whole CJK run becomes one token, so ``recall("北京")`` never matches "...北京...".
# We split CJK runs into overlapping bigrams (other scripts' words stay whole) on
# the indexed text and the query, so the two sides line up, two-character words
# match exactly, and bm25 still ranks. The same extractor drives the LIKE
# fallback, so a natural-language CJK query matches by its bigrams there too.

# CJK Unified Ideographs (+ Ext. A, Compatibility), kana, and Hangul.
_CJK = "㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
_CJK_RE = re.compile(rf"[{_CJK}]")
# A CJK run, or a run of word characters in any other script (ASCII, accented
# Latin, Cyrillic, Greek, ...) — those use spaces, so they stay whole words.
_PIECE_RE = re.compile(rf"[{_CJK}]+|[^\W{_CJK}]+")


def _bigrams(run: str) -> list[str]:
    """Overlapping 2-grams of a CJK run (the run itself when 1–2 chars long)."""
    if len(run) <= 2:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def _terms(text: str) -> list[str]:
    """Split text into search terms: word-runs whole, CJK runs as bigrams."""
    out: list[str] = []
    for piece in _PIECE_RE.findall(text.lower()):
        if _CJK_RE.match(piece):
            out.extend(_bigrams(piece))
        else:
            out.append(piece)
    return out


def _index_text(text: str) -> str:
    """The bigram-segmented form stored in the FTS ``search`` column."""
    return " ".join(_terms(text))


def _fts5_available() -> bool:
    """Probe whether this SQLite build has the FTS5 extension."""
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        finally:
            con.close()
        return True
    except sqlite3.OperationalError:
        return False


# ``text`` is stored for display only (UNINDEXED); ``search`` holds the
# bigram-segmented form that the default tokenizer actually indexes.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
    session_id UNINDEXED,
    run_id UNINDEXED,
    text UNINDEXED,
    search,
    when_ts UNINDEXED
);
"""

_PLAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS archive_docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    text TEXT NOT NULL,
    when_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_archive_docs_sid ON archive_docs(session_id);
"""


class SQLiteArchiveStore(SQLiteStore):
    """Default :class:`ArchiveStore`: stdlib SQLite with FTS5 full-text search.

    Each completed run appends its own messages as per-message rows keyed by
    ``(session_id, run_id)``. Ingest is **replace-on-run** so a resumed
    completion re-ingesting the same ``run_id`` is idempotent, while distinct
    runs accumulate across a session; a run with no id gets a unique key so it is
    still retained. Search ranks with bm25 over a CJK-aware bigram index (so
    scripts without whitespace word boundaries match too) when FTS5 is available,
    and falls back to a recency-ordered ``LIKE`` scan otherwise.
    """

    def __init__(
        self, path: str | os.PathLike[str] = f"{_DEFAULT_ROOT}/{_ARCHIVE_FILENAME}"
    ) -> None:
        self._use_fts = _fts5_available()
        schema = _FTS_SCHEMA if self._use_fts else _PLAIN_SCHEMA
        p = str(path)
        if p != ":memory:":
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(p, schema)
        self._table = "archive_fts" if self._use_fts else "archive_docs"

    async def ingest(
        self,
        session_id: str | None,
        entries: list[TranscriptEntry],
        *,
        run_id: str | None = None,
    ) -> None:
        docs = _archive_docs(entries)
        if not docs:
            return
        sid = session_id or f"run-{uuid.uuid4().hex}"
        rid = run_id or uuid.uuid4().hex
        now = time.time()
        table = self._table
        rows: list[tuple[Any, ...]]
        if self._use_fts:
            insert = (
                f"INSERT INTO {table} (session_id, run_id, text, search, when_ts) "
                "VALUES (?, ?, ?, ?, ?)"
            )
            rows = [(sid, rid, d, _index_text(d), now) for d in docs]
        else:
            insert = (
                f"INSERT INTO {table} (session_id, run_id, text, when_ts) "
                "VALUES (?, ?, ?, ?)"
            )
            rows = [(sid, rid, d, now) for d in docs]

        def _impl() -> None:
            conn = self._connect()
            try:
                # Replace-on-run: a re-ingested run_id supersedes its old rows,
                # but distinct runs in a session accumulate.
                conn.execute(
                    f"DELETE FROM {table} WHERE session_id = ? AND run_id = ?",
                    (sid, rid),
                )
                conn.executemany(insert, rows)
                conn.commit()
            finally:
                self._release(conn)

        await self._run(_impl)

    async def search(self, query: str, k: int = 5) -> list[ArchiveHit]:
        terms = _terms(query)
        if not terms:
            return []

        if self._use_fts:
            # Quote each term so a CJK bigram or ASCII word is one FTS phrase;
            # OR them and let bm25 rank by how many distinct terms each row hits.
            match = " OR ".join(f'"{t}"' for t in terms)

            def _fts() -> list[ArchiveHit]:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT session_id, when_ts, text, bm25(archive_fts) AS score "
                        "FROM archive_fts WHERE archive_fts MATCH ? "
                        "ORDER BY score LIMIT ?",
                        (match, k),
                    ).fetchall()
                finally:
                    self._release(conn)
                # bm25 returns lower = better; report -bm25 so higher = better.
                return [
                    ArchiveHit(
                        session_id=r["session_id"],
                        when=r["when_ts"],
                        text=r["text"],
                        score=-float(r["score"]),
                    )
                    for r in rows
                ]

            return await self._run(_fts)

        clause = " OR ".join(["text LIKE ?"] * len(terms))
        params: list[Any] = [f"%{t}%" for t in terms] + [k]

        def _like() -> list[ArchiveHit]:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT session_id, when_ts, text FROM archive_docs "
                    f"WHERE {clause} ORDER BY when_ts DESC LIMIT ?",
                    params,
                ).fetchall()
            finally:
                self._release(conn)
            return [
                ArchiveHit(
                    session_id=r["session_id"],
                    when=r["when_ts"],
                    text=r["text"],
                    score=0.0,
                )
                for r in rows
            ]

        return await self._run(_like)


# ---------------------------------------------------------------------------
# Curation side-queries — a tool-less, plugin-less sub-agent run via
# ``Runner.run``. It dogfoods structured output + the provider chain and cannot
# recurse (the sub-agent has no Memory plugin). Runner/Agent are imported lazily
# to avoid an import cycle (plugins -> runner -> loop -> plugins.base).
# ---------------------------------------------------------------------------


class _ExtractedFacts(BaseModel):
    facts: list[str] = Field(
        default_factory=list,
        description=(
            "New durable facts worth remembering long-term (stable preferences, "
            "corrections, lasting details about the user or project). Empty if "
            "there is nothing new worth keeping."
        ),
    )


class _ConsolidatedNotes(BaseModel):
    facts: list[str] = Field(
        default_factory=list,
        description="The rewritten, deduplicated, shorter set of notes.",
    )


_EXTRACT_INSTRUCTIONS = (
    "You curate an agent's long-term memory. From a conversation, identify only "
    "facts that will still matter in future, unrelated sessions: the user's "
    "stable preferences, corrections they made, and durable facts about them or "
    "their project. Ignore transient task details, one-off requests, and "
    "anything already covered by the current notes. Return each kept fact as a "
    "short, self-contained line. If nothing qualifies, return an empty list."
)

_CONSOLIDATE_INSTRUCTIONS = (
    "You compress an agent's long-term notes. Merge duplicates and near-"
    "duplicates, drop the least important entries, and keep durable preferences "
    "and facts. Preserve meaning; be concise. Return the rewritten notes as a "
    "list of short, self-contained lines that fit the requested budget."
)

_SUMMARIZE_INSTRUCTIONS = (
    "You answer from an agent's memory archive. Given a question and some "
    "retrieved excerpts from past conversations, summarize only what is "
    "relevant to the question, concisely. If nothing is relevant, say so "
    "plainly."
)


def _render_transcript(entries: list[TranscriptEntry]) -> str:
    lines: list[str] = []
    for m in entries_to_messages(entries):
        if m.role not in ("user", "assistant"):
            continue
        text = text_of(m.content).strip()
        if text:
            lines.append(f"{m.role.upper()}: {text}")
    return "\n".join(lines)


async def _extract(
    entries: list[TranscriptEntry],
    current_notes: str,
    model: "str | Provider | list[str | Provider]",
) -> list[str]:
    from ..agent import Agent
    from ..providers import ModelSettings
    from ..runner import Runner

    convo = _render_transcript(entries)
    if not convo.strip():
        return []
    agent: Agent[Any] = Agent(
        name="memory-extractor",
        model=model,
        instructions=_EXTRACT_INSTRUCTIONS,
        output_type=_ExtractedFacts,
        settings=ModelSettings(temperature=0),
    )
    prompt = (
        f"## Current notes (do NOT repeat these)\n{current_notes or '(empty)'}\n\n"
        f"## Conversation\n{convo}\n\n"
        "Extract only NEW durable facts not already in the notes."
    )
    result = await Runner.run(agent, prompt)
    facts = getattr(result.output, "facts", []) or []
    return [n for f in facts if (n := _normalize_fact(f))]


async def _consolidate(
    body: str,
    max_chars: int,
    model: "str | Provider | list[str | Provider]",
) -> list[str]:
    from ..agent import Agent
    from ..providers import ModelSettings
    from ..runner import Runner

    agent: Agent[Any] = Agent(
        name="memory-consolidator",
        model=model,
        instructions=_CONSOLIDATE_INSTRUCTIONS,
        output_type=_ConsolidatedNotes,
        settings=ModelSettings(temperature=0),
    )
    prompt = (
        f"Rewrite these notes to fit within roughly {max_chars} characters "
        f"total.\n\n{body}"
    )
    result = await Runner.run(agent, prompt)
    facts = getattr(result.output, "facts", []) or []
    return [n for f in facts if (n := _normalize_fact(f))]


async def _summarize(
    hits: list[ArchiveHit],
    query: str,
    model: "str | Provider | list[str | Provider]",
) -> str:
    from ..agent import Agent
    from ..providers import ModelSettings
    from ..runner import Runner

    joined = "\n\n".join(f"(session {h.session_id})\n{h.text}" for h in hits)
    agent: Agent[Any] = Agent(
        name="memory-recall",
        model=model,
        instructions=_SUMMARIZE_INSTRUCTIONS,
        settings=ModelSettings(temperature=0),
    )
    prompt = f"## Question\n{query}\n\n## Retrieved memories\n{joined}"
    result = await Runner.run(agent, prompt)
    return str(result.output)


# ---------------------------------------------------------------------------
# The plugin
# ---------------------------------------------------------------------------


_REMEMBER_DESCRIPTION = (
    "Save a durable fact to long-term memory so you can recall it in future "
    "sessions. Use it for stable preferences, corrections, or lasting details "
    "about the user or project — not transient task state. Pass one concise, "
    "self-contained fact per call."
)

_FORGET_DESCRIPTION = (
    "Remove a fact from long-term memory when it is wrong or no longer true. "
    "Pass text matching the note to drop."
)

_RECALL_DESCRIPTION = (
    "Search long-term memory (the archive of past conversations) for anything "
    "relevant to a query. Use it when the user refers to an earlier "
    "conversation, or you need context beyond your current notes."
)


def _make_remember(notes: NotesStore) -> Tool:
    @tool(name="remember", description=_REMEMBER_DESCRIPTION)
    async def remember(fact: Annotated[str, "The durable fact to remember."]) -> str:
        before = await notes.raw()
        await notes.add(fact)
        if await notes.raw() == before:
            return "Already in your notes — nothing to add."
        return "Remembered. It will be available in future sessions."

    return remember


def _make_forget(notes: NotesStore) -> Tool:
    @tool(name="forget", description=_FORGET_DESCRIPTION)
    async def forget(fact: Annotated[str, "Text matching the note to remove."]) -> str:
        before = await notes.raw()
        await notes.remove(fact)
        if await notes.raw() == before:
            return "No matching note found to forget."
        return "Forgotten."

    return forget


def _make_recall(plugin: "Memory", archive: ArchiveStore) -> Tool:
    @tool(name="recall", description=_RECALL_DESCRIPTION)
    async def recall(
        ctx: RunContext[Any],
        query: Annotated[str, "What to look for in past conversations."],
    ) -> str:
        hits = await archive.search(query, plugin.recall_k)
        if not hits:
            return "(nothing relevant found in long-term memory)"
        if plugin.summarize_recall:
            try:
                return await _summarize(hits, query, plugin._resolve_model(ctx))
            except Exception:
                # Fail-open: fall back to raw hits, so this degrades output but
                # doesn't fail the tool — WARNING, not ERROR (keep traceback).
                logger.warning(
                    "memory: recall summary failed; returning raw hits",
                    exc_info=True,
                )
        return "\n\n".join(f"- {h.text}" for h in hits)

    return recall


def _build_instructions(has_archive: bool) -> str:
    parts = [
        "You have long-term memory that persists across sessions.",
        "- Your durable NOTES are shown below and are always in context — they "
        "hold the user's stable preferences, facts about them, and context "
        "worth keeping.",
        "- Call `remember(fact)` to save a new durable fact (a preference, a "
        "correction, a stable detail). Keep each fact short and self-contained.",
        "- Call `forget(fact)` to remove a note that is wrong or no longer true.",
    ]
    if has_archive:
        parts.append(
            "- Call `recall(query)` to search past conversations when the user "
            "refers to something earlier or you need context not in your notes."
        )
    parts.append(
        "Save durable facts proactively, but don't record transient task "
        "details or things that won't matter later."
    )
    return "\n".join(parts)


@dataclass
class Memory:
    """Tiered cross-session memory: hot **Notes** + cold **Archive**.

    ``Memory("./dir")`` (or ``Memory()``) builds both default stores under that
    root. Pass ``notes=`` / ``archive=`` to supply custom backends; set
    ``archive=None`` for a notes-only memory with no ``recall`` tool.

    Fields:
        notes: A :class:`NotesStore`, or a root directory path under which the
            default :class:`FileNotesStore` (+ :class:`SQLiteArchiveStore`) are
            created.
        archive: A :class:`ArchiveStore`; ``None`` to disable the cold tier.
            Left unset, the default :class:`SQLiteArchiveStore` is built under
            the notes root when ``notes`` is a path (and omitted otherwise).
        auto_extract: When ``True`` (default), the plugin promotes durable facts
            from the conversation into Notes at the end of each run (and
            consolidates Notes that exceed the store's budget). This adds one
            model call at run end; set ``False`` for purely manual notes.
        summarize_recall: When ``True`` (default), ``recall`` returns a cheap
            model-written summary of the hits rather than the raw excerpts.
        recall_k: Number of archive hits ``recall`` retrieves.
        model: Model for the curation side-queries. ``None`` (default) reuses
            the host agent's model.
        name: Plugin name.
    """

    notes: "NotesStore | str | os.PathLike[str]" = _DEFAULT_ROOT
    archive: "ArchiveStore | None" = _DEFAULT_ARCHIVE
    auto_extract: bool = True
    summarize_recall: bool = True
    recall_k: int = 5
    model: "str | Provider | list[str | Provider] | None" = None
    name: str = "memory"

    def __post_init__(self) -> None:
        # ``archive`` left untouched → build the default archive under the notes
        # root (when ``notes`` is a path); an explicit ``archive=None`` always
        # disables the cold tier.
        archive_is_default = self.archive is _DEFAULT_ARCHIVE
        if isinstance(self.notes, (str, os.PathLike)):
            root = Path(self.notes)
            self.notes = FileNotesStore(root / _NOTES_FILENAME)
            if archive_is_default:
                self.archive = SQLiteArchiveStore(root / _ARCHIVE_FILENAME)
        if self.archive is _DEFAULT_ARCHIVE:
            # Custom notes store (no root to anchor a default archive) and no
            # explicit archive → notes-only, matching the old behavior.
            self.archive = None

    @property
    def has_archive(self) -> bool:
        """Whether the cold-tier searchable archive (the ``recall`` tool) is on.

        ``False`` when constructed with ``archive=None`` (notes-only). Lets
        callers branch — e.g. a UI that only shows a "search history" affordance
        when recall is available — without poking at the sentinel default.
        """
        return self.archive is not None

    def _resolve_model(
        self, ctx: RunContext[Any]
    ) -> "str | Provider | list[str | Provider]":
        # ``self.model`` overrides; otherwise reuse the host agent's model. Both
        # callers (the recall tool and the RunCompleted hook) pass a live
        # context, and ``RunContext.agent`` / ``Agent.model`` are always set.
        return self.model if self.model is not None else ctx.agent.model

    async def setup(self) -> PluginInstance:
        notes = cast(NotesStore, self.notes)
        archive = self.archive

        tools: list[Tool] = [_make_remember(notes), _make_forget(notes)]
        if archive is not None:
            tools.append(_make_recall(self, archive))

        guidance = _build_instructions(archive is not None)
        instructions = f"{guidance}\n\n{await notes.render()}"

        return PluginInstance(
            tools=tools,
            instructions=instructions,
            hooks=self._make_hooks(notes, archive),
        )

    def _make_hooks(
        self, notes: NotesStore, archive: "ArchiveStore | None"
    ) -> AgentHooks:
        hooks = AgentHooks()
        finalized = False

        @hooks.on(RunCompleted)
        async def _on_completed(ev: RunCompleted, ctx: RunContext[Any]) -> None:
            # Every hook receives the run's live RunContext; it carries the
            # ``session_id`` and active agent, neither of which is on the event
            # or :class:`~lovia.RunResult`.
            nonlocal finalized
            # Guard against any double-dispatch; RunCompleted is once per run.
            if finalized:
                return
            finalized = True
            entries = ev.result.entries

            if archive is not None:
                try:
                    await archive.ingest(ctx.session_id, entries, run_id=ctx.run_id)
                except Exception:
                    # Best-effort background curation: the run already
                    # completed, so a failure here is WARNING, not ERROR.
                    logger.warning("memory: archive ingest failed", exc_info=True)

            if self.auto_extract:
                await self._curate_notes(notes, entries, self._resolve_model(ctx))

        return hooks

    async def _curate_notes(
        self,
        notes: NotesStore,
        entries: list[TranscriptEntry],
        model: "str | Provider | list[str | Provider]",
    ) -> None:
        # 1) Promote new durable facts into Notes.
        try:
            current = await notes.raw()
            for fact in await _extract(entries, current, model):
                await notes.add(fact)
        except Exception:
            # Best-effort curation (run already completed) — WARNING, not ERROR.
            logger.warning("memory: note extraction failed", exc_info=True)
            return
        # 2) Consolidate when Notes exceed the store's char budget.
        try:
            body = await notes.raw()
            max_chars = getattr(notes, "max_chars", None)
            if max_chars and len(body) > max_chars:
                facts = await _consolidate(body, max_chars, model)
                if facts:
                    await notes.replace(_format_facts(facts))
        except Exception:
            # Best-effort curation (run already completed) — WARNING, not ERROR.
            logger.warning("memory: note consolidation failed", exc_info=True)


__all__ = [
    "ArchiveHit",
    "ArchiveStore",
    "FileNotesStore",
    "Memory",
    "NotesStore",
    "SQLiteArchiveStore",
]
