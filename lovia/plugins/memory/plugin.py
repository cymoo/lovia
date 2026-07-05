"""Memory plugin: tiered, cross-session memory for an agent.

``Memory`` gives an agent a long-term memory that survives across runs and
sessions, built from two tiers and three verbs the model already understands:

* **Notes** (the *hot* tier) — a tiny, char-budgeted list of facts that is
  **always injected** into the system prompt: the user's stable preferences,
  durable facts, important context. The model curates it with
  ``remember(fact)`` / ``forget(fact)``, and (when ``auto_curate``) the plugin
  promotes durable facts into it automatically at the end of each run.
* **Archive** (the *cold* tier) — a searchable index of past conversations,
  pulled in on demand via ``recall(query)``.

The tiers sit behind two deliberately narrow seams. The hot tier is a
:class:`NotesStore` (``load``/``save`` a fact list — all policy such as
normalization, dedup, rendering, and budgeting lives here in the plugin, so a
custom store is pure persistence). The cold tier is an
:class:`~.index.Index` (``add``/``remove``/``search`` over plain
:class:`~.index.Doc` — no transcript knowledge required). Escalate recall
quality by saying one more fact per step::

    Memory("./memory")                            # stdlib FTS + LLM query expansion
    Memory("./memory", embedder=OpenAIEmbedder()) # auto-hybrid: keyword | vector, RRF
    Memory("./memory", index=my_index)            # bring your own retrieval engine

The zero-dependency default leans on the one model that is always present in
an agent framework — the LLM itself: queries are expanded with synonyms and
translations before hitting the lexical index (``expand_query="auto"`` turns
this off when a semantic arm is configured), and at run end a single digest
call both promotes durable facts into Notes and writes a self-contained
episode summary into the archive, where it searches far better than raw chat
fragments.

Idempotency is carried by the data: message docs get deterministic ids
(``run_id:seq``), so a resumed run that re-ingests simply upserts. This fits
lovia because the transcript is durable and compaction is view-only — nothing
is ever lost from the record, so end-of-run work is pure curation, and the
plugin hooks only :class:`~lovia.events.RunCompleted` (``result.entries`` is
the run's own complete transcript).

Backends are long-lived and shared by every run (held on the plugin, never
rebuilt per run, never closed by the plugin); :meth:`Memory.setup` only
assembles the per-run tools, instructions, and the ``RunCompleted`` hook.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import EllipsisType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Literal,
    Protocol,
    cast,
    runtime_checkable,
)

from pydantic import BaseModel, Field

from ...events import RunCompleted
from ...exceptions import UserError
from ...hooks import AgentHooks
from ...parts import text_of
from ...run_context import RunContext
from ...tools import Tool, tool
from ...transcript import TranscriptEntry, entries_to_messages
from ..base import PluginInstance
from .index import Doc, Hit, Index, KeywordIndex
from .vector import Embedder, VectorIndex

if TYPE_CHECKING:
    from ...providers import Provider

logger = logging.getLogger(__name__)

_DEFAULT_ROOT = "./.lovia/memory"
_NOTES_FILENAME = "MEMORY.md"
_ARCHIVE_FILENAME = "archive.db"
_VECTORS_FILENAME = "vectors.db"
_DEFAULT_NOTES_BUDGET = 2000


# ---------------------------------------------------------------------------
# Notes policy helpers — the canonical hot-tier form is a list of short facts,
# persisted as a ``- fact`` bullet list (markdown-native, model-friendly, and
# human-editable). All of this is plugin policy shared by every NotesStore.
# ---------------------------------------------------------------------------


def _normalize_fact(fact: str) -> str:
    """Collapse a fact to a single trimmed line (notes are one fact per line)."""
    return " ".join(fact.split())


def _parse_facts(body: str) -> list[str]:
    """Parse a notes body (``- fact`` per line) into its facts."""
    facts: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            fact = stripped[2:].strip()
            if fact:
                facts.append(fact)
    return facts


def _format_facts(facts: list[str]) -> str:
    """Render facts to the canonical bullet-list body."""
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
# Hot tier: NotesStore
# ---------------------------------------------------------------------------


@runtime_checkable
class NotesStore(Protocol):
    """The hot-tier seam: pure persistence of a fact list.

    Two methods, no policy: normalization, dedup, fuzzy removal, budgeting,
    and prompt rendering all live in the plugin, identically for every store.
    Read-modify-write cycles are serialized by the plugin; a store only needs
    each individual ``load``/``save`` to be safe.
    """

    async def load(self) -> list[str]:
        """Return the stored facts (empty when nothing is stored yet)."""
        ...

    async def save(self, facts: list[str]) -> None:
        """Persist ``facts``, replacing the previous list."""
        ...


class FileNotesStore:
    """Default :class:`NotesStore`: one markdown file, one ``- fact`` per line.

    The file is human-editable (non-bullet lines are ignored on load). Writes
    are atomic via a temp-file rename, so readers never see a half-written
    body even if the process dies mid-save.
    """

    def __init__(
        self, path: str | os.PathLike[str] = f"{_DEFAULT_ROOT}/{_NOTES_FILENAME}"
    ) -> None:
        self._path = Path(path)

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

    async def load(self) -> list[str]:
        return _parse_facts(await asyncio.to_thread(self._read))

    async def save(self, facts: list[str]) -> None:
        await asyncio.to_thread(self._write, _format_facts(facts))


# ---------------------------------------------------------------------------
# Transcript → docs (the only place the cold tier meets lovia's data model)
# ---------------------------------------------------------------------------


def _message_texts(entries: list[TranscriptEntry]) -> list[tuple[str, str]]:
    """The non-empty user/assistant ``(role, text)`` pairs of a transcript."""
    pairs: list[tuple[str, str]] = []
    for m in entries_to_messages(entries):
        if m.role not in ("user", "assistant"):
            continue
        text = text_of(m.content).strip()
        if text:
            pairs.append((m.role, text))
    return pairs


def _render_transcript(entries: list[TranscriptEntry]) -> str:
    return "\n".join(
        f"{role.upper()}: {text}" for role, text in _message_texts(entries)
    )


def _hit_line(hit: Hit) -> str:
    """Render a hit for the model: date-stamped so answers can be grounded in time."""
    if hit.doc.when > 0:
        day = time.strftime("%Y-%m-%d", time.localtime(hit.doc.when))
        return f"[{day}] {hit.doc.text}"
    return hit.doc.text


# ---------------------------------------------------------------------------
# Curation side-queries — tool-less, plugin-less sub-agent runs via
# ``Runner.run``. They dogfood structured output + the provider chain and
# cannot recurse (the sub-agent has no Memory plugin). Runner/Agent are
# imported lazily to avoid an import cycle (plugins -> runner -> loop ->
# plugins.base).
# ---------------------------------------------------------------------------


class _RunDigest(BaseModel):
    facts: list[str] = Field(
        default_factory=list,
        description=(
            "New durable facts worth remembering long-term (stable preferences, "
            "corrections, lasting details about the user or project). Empty if "
            "there is nothing new worth keeping."
        ),
    )
    summary: str = Field(
        default="",
        description=(
            "A short, self-contained summary of the conversation for the "
            "long-term archive. Empty if the conversation has no content worth "
            "archiving."
        ),
    )


class _ConsolidatedNotes(BaseModel):
    facts: list[str] = Field(
        default_factory=list,
        description="The rewritten, deduplicated, shorter set of notes.",
    )


class _ExpandedQuery(BaseModel):
    terms: list[str] = Field(
        default_factory=list,
        description="Short search terms: synonyms, rephrasings, translations.",
    )


_DIGEST_INSTRUCTIONS = (
    "You curate an agent's long-term memory. From a conversation, produce two "
    "things.\n"
    "1. facts: only facts that will still matter in future, unrelated sessions "
    "— the user's stable preferences, corrections they made, and durable facts "
    "about them or their project. Ignore transient task details, one-off "
    "requests, and anything already covered by the current notes. Each fact is "
    "one short, self-contained line in the conversation's dominant language. "
    "If nothing qualifies, return an empty list.\n"
    "2. summary: a few sentences capturing what the conversation was about — "
    "topics, decisions, and outcomes — written to be found and understood on "
    "its own later, in the conversation's dominant language. Quote names, "
    "numbers, and identifiers exactly as they appeared. Empty if the "
    "conversation is trivial (greetings, tests, nothing worth recalling)."
)

_CONSOLIDATE_INSTRUCTIONS = (
    "You compress an agent's long-term notes. Merge duplicates and near-"
    "duplicates, drop the least important entries, and keep durable preferences "
    "and facts. Preserve meaning and keep each note in its original language; "
    "be concise. Return the rewritten notes as a list of short, self-contained "
    "lines that fit the requested budget."
)

_EXPAND_INSTRUCTIONS = (
    "You expand search queries for a keyword search over past conversations. "
    "Given a query, return up to 10 short terms that could appear verbatim in "
    "relevant text: synonyms, alternate phrasings, and translations into the "
    "other languages the user plausibly writes in (at minimum English and the "
    "query's own language). For category words, also give a few common "
    "concrete instances, since the text likely names the specific thing (pet "
    "→ dog, cat; vehicle → car, bike). Prefer concrete content words; never "
    "translate or alter codes, numbers, or identifiers; do not repeat words "
    "already in the query. Return only the terms."
)

_SUMMARIZE_INSTRUCTIONS = (
    "You answer from an agent's memory archive. Given a question and some "
    "retrieved excerpts from past conversations, summarize only what is "
    "relevant to the question, concisely, in the question's language. The "
    "excerpts are date-stamped — mention when something happened if it helps. "
    "If nothing is relevant, say so plainly."
)


async def _digest(
    entries: list[TranscriptEntry],
    current_notes: str,
    model: "str | Provider | list[str | Provider]",
) -> _RunDigest:
    from ...agent import Agent
    from ...providers import ModelSettings
    from ...runner import Runner

    convo = _render_transcript(entries)
    if not convo.strip():
        return _RunDigest()
    agent: Agent[Any] = Agent(
        name="memory-digest",
        model=model,
        instructions=_DIGEST_INSTRUCTIONS,
        output_type=_RunDigest,
        settings=ModelSettings(temperature=0),
    )
    prompt = (
        f"## Current notes (do NOT repeat these in facts)\n"
        f"{current_notes or '(empty)'}\n\n"
        f"## Conversation\n{convo}"
    )
    result = await Runner.run(agent, prompt)
    digest = cast(_RunDigest, result.output)
    digest.facts = [n for f in digest.facts if (n := _normalize_fact(f))]
    return digest


async def _consolidate(
    body: str,
    max_chars: int,
    model: "str | Provider | list[str | Provider]",
) -> list[str]:
    from ...agent import Agent
    from ...providers import ModelSettings
    from ...runner import Runner

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


async def _expand(
    query: str,
    model: "str | Provider | list[str | Provider]",
) -> list[str]:
    from ...agent import Agent
    from ...providers import ModelSettings
    from ...runner import Runner

    agent: Agent[Any] = Agent(
        name="memory-query-expander",
        model=model,
        instructions=_EXPAND_INSTRUCTIONS,
        output_type=_ExpandedQuery,
        settings=ModelSettings(temperature=0),
    )
    result = await Runner.run(agent, f"Query: {query}")
    terms = getattr(result.output, "terms", []) or []
    return [n for t in terms if (n := _normalize_fact(t))]


async def _summarize(
    hits: list[Hit],
    query: str,
    model: "str | Provider | list[str | Provider]",
) -> str:
    from ...agent import Agent
    from ...providers import ModelSettings
    from ...runner import Runner

    joined = "\n\n".join(_hit_line(h) for h in hits)
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


def _build_instructions(has_index: bool) -> str:
    parts = [
        "You have long-term memory that persists across sessions.",
        "- Your durable NOTES are shown below and are always in context — they "
        "hold the user's stable preferences, facts about them, and context "
        "worth keeping.",
        "- Call `remember(fact)` to save a new durable fact (a preference, a "
        "correction, a stable detail). Keep each fact short and self-contained.",
        "- Call `forget(fact)` to remove a note that is wrong or no longer true.",
    ]
    if has_index:
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

    ``Memory()`` / ``Memory("./dir")`` is fully zero-config: markdown Notes and
    a stdlib keyword index under that root, with LLM query expansion covering
    the lexical gaps. Each further capability is one more argument:

    * ``embedder=`` — upgrade the default index to keyword|vector hybrid
      (Reciprocal Rank Fusion); expansion turns off automatically.
    * ``index=`` — replace the retrieval engine outright (any
      :class:`~.index.Index`); ``index=None`` disables the cold tier and the
      ``recall`` tool.
    * ``notes=`` — replace the hot-tier persistence (any :class:`NotesStore`).

    :meth:`remember` and :meth:`forget` are also public methods, so code can
    seed or clean Notes without a model in the loop.
    """

    root: str | os.PathLike[str] = _DEFAULT_ROOT
    """Directory for the default stores. Ignored for a tier whose store is
    passed explicitly."""

    notes: "NotesStore | None" = None
    """Custom hot-tier store; the default builds ``MEMORY.md`` under
    ``root``."""

    index: "Index | EllipsisType | None" = ...
    """Custom cold-tier index. Leave unset for the default (keyword, or
    hybrid when ``embedder`` is given); ``None`` disables recall."""

    embedder: "Embedder | None" = None
    """Adds a semantic arm to the *default* index. Mutually exclusive with
    ``index=`` — a custom index embeds however it wants."""

    auto_curate: bool = True
    """When ``True`` (default), one model call at run end promotes durable
    facts into Notes and writes an episode summary into the archive (plus a
    consolidation call when Notes outgrow their budget). ``False`` = purely
    manual memory."""

    expand_query: "bool | Literal['auto']" = "auto"
    """Expand recall queries with LLM-generated synonyms and translations
    before searching. ``"auto"`` (default) enables this only for the default
    keyword-only index — lexical search needs the help; a semantic arm
    doesn't."""

    summarize_recall: bool = True
    """When ``True`` (default), ``recall`` returns a model-written summary
    of the hits rather than the raw excerpts."""

    recall_k: int = 5
    """Number of hits ``recall`` retrieves."""

    notes_budget: int = _DEFAULT_NOTES_BUDGET
    """Char budget for Notes; exceeding it triggers consolidation and is
    what the meter in the prompt reports."""

    model: "str | Provider | list[str | Provider] | None" = None
    """Model for the curation side-queries. ``None`` (default) reuses the
    host agent's model."""

    name: str = "memory"
    """Plugin name."""

    def __post_init__(self) -> None:
        if self.notes is None:
            self.notes = FileNotesStore(Path(self.root) / _NOTES_FILENAME)
        if isinstance(self.index, EllipsisType):
            keyword = KeywordIndex(Path(self.root) / _ARCHIVE_FILENAME)
            if self.embedder is not None:
                vector = VectorIndex(Path(self.root) / _VECTORS_FILENAME, self.embedder)
                self.index = keyword | vector
                self._lexical_only = False
            else:
                self.index = keyword
                self._lexical_only = True
        else:
            if self.embedder is not None:
                raise UserError(
                    "Memory: pass either embedder= or index=, not both",
                    hint=(
                        "embedder= only upgrades the default index; a custom "
                        "index handles its own embedding (compose one with "
                        "KeywordIndex(...) | VectorIndex(..., embedder))."
                    ),
                )
            # A custom engine's strength is unknown; assume it doesn't need
            # lexical help unless the user forces expand_query=True.
            self._lexical_only = False
        # Serializes read-modify-write cycles on Notes (tools + curation).
        self._notes_lock = asyncio.Lock()

    def _resolve_model(
        self, ctx: RunContext[Any]
    ) -> "str | Provider | list[str | Provider]":
        # ``self.model`` overrides; otherwise reuse the host agent's model. A
        # mid-run host always has one (its providers resolved at run start),
        # but a hand-built RunContext in a unit test may not — raise a clear
        # error rather than assert, so the gap is diagnosable everywhere.
        if self.model is not None:
            return self.model
        model = ctx.agent.model
        if model is None:
            raise UserError(
                "Memory has no model to run on: the host agent has none configured",
                hint="pass Memory(model=...) or set the host Agent's model",
            )
        return model

    def _should_expand(self) -> bool:
        if self.expand_query == "auto":
            return self._lexical_only
        return bool(self.expand_query)

    # -- notes policy (shared by tools and curation) --------------------------

    def _notes_store(self) -> NotesStore:
        return cast(NotesStore, self.notes)

    async def _render_notes(self) -> str:
        body = _format_facts(await self._notes_store().load())
        meter = _meter(len(body), self.notes_budget)
        if not body:
            return f"NOTES {meter}\n(empty — use `remember` to save durable facts)"
        return f"NOTES {meter}\n{body}"

    async def _add_facts(self, new: list[str]) -> int:
        """Merge normalized facts into Notes (case-insensitive dedup)."""
        notes = self._notes_store()
        async with self._notes_lock:
            facts = await notes.load()
            seen = {f.lower() for f in facts}
            added = 0
            for fact in new:
                norm = _normalize_fact(fact)
                if not norm or norm.lower() in seen:
                    continue
                facts.append(norm)
                seen.add(norm.lower())
                added += 1
            if added:
                await notes.save(facts)
        return added

    async def remember(self, fact: str) -> bool:
        """Add one durable fact to Notes; ``False`` if it was already there.

        Backs the model's ``remember`` tool; call it directly to seed memories
        programmatically (onboarding data, migrations, an admin UI).
        """
        return await self._add_facts([fact]) == 1

    async def forget(self, fact: str) -> bool:
        """Remove the best-matching note; ``False`` if nothing matched.

        Matching is exact → case-insensitive → substring, same as the model's
        ``forget`` tool, which this backs.
        """
        norm = _normalize_fact(fact)
        if not norm:
            return False
        notes = self._notes_store()
        async with self._notes_lock:
            facts = await notes.load()
            kept = _drop_fact(facts, norm)
            if len(kept) == len(facts):
                return False
            await notes.save(kept)
        return True

    async def notes_body(self) -> str:
        """The current Notes as their canonical ``- fact`` body (may be ``""``).

        The read half of the editing seam; :meth:`replace_notes` is the write
        half. The web UI's memory editor is built on the pair.
        """
        return _format_facts(await self._notes_store().load())

    async def replace_notes(self, body: str) -> str:
        """Replace Notes wholesale with the facts parsed from ``body``.

        The bulk counterpart to :meth:`remember` / :meth:`forget`, for editor
        flows (the web UI, imports): ``body`` uses the same ``- fact`` per line
        form :meth:`notes_body` returns. Non-bullet lines are ignored, facts
        are normalized and case-insensitively deduplicated — the same policy
        every other Notes write applies. Returns the canonical body stored.
        """
        facts: list[str] = []
        seen: set[str] = set()
        for fact in _parse_facts(body):
            norm = _normalize_fact(fact)
            if norm and norm.lower() not in seen:
                facts.append(norm)
                seen.add(norm.lower())
        async with self._notes_lock:
            await self._notes_store().save(facts)
        return _format_facts(facts)

    # -- setup -----------------------------------------------------------------

    async def setup(self) -> PluginInstance:
        index = cast("Index | None", self.index)

        tools: list[Tool] = [self._make_remember(), self._make_forget()]
        if index is not None:
            tools.append(self._make_recall(index))

        guidance = _build_instructions(index is not None)
        instructions = f"{guidance}\n\n{await self._render_notes()}"

        return PluginInstance(
            tools=tools,
            instructions=instructions,
            hooks=self._make_hooks(index),
        )

    # -- tools -------------------------------------------------------------------

    def _make_remember(self) -> Tool:
        plugin = self

        @tool(name="remember", description=_REMEMBER_DESCRIPTION)
        async def remember(
            fact: Annotated[str, "The durable fact to remember."],
        ) -> str:
            if await plugin.remember(fact):
                return "Remembered. It will be available in future sessions."
            return "Already in your notes — nothing to add."

        return remember

    def _make_forget(self) -> Tool:
        plugin = self

        @tool(name="forget", description=_FORGET_DESCRIPTION)
        async def forget(
            fact: Annotated[str, "Text matching the note to remove."],
        ) -> str:
            if await plugin.forget(fact):
                return "Forgotten."
            return "No matching note found to forget."

        return forget

    def _make_recall(self, index: Index) -> Tool:
        plugin = self

        @tool(name="recall", description=_RECALL_DESCRIPTION)
        async def recall(
            ctx: RunContext[Any],
            query: Annotated[str, "What to look for in past conversations."],
        ) -> str:
            search_query = query
            if plugin._should_expand():
                try:
                    terms = await _expand(query, plugin._resolve_model(ctx))
                    if terms:
                        # The expansion terms ride along with the original
                        # query; any Index benefits without knowing about it.
                        search_query = f"{query} {' '.join(terms)}"
                except Exception:
                    # Fail-open: the raw query still searches.
                    logger.warning(
                        "memory: query expansion failed; searching the raw query",
                        exc_info=True,
                    )
            hits = await index.search(search_query, plugin.recall_k)
            if not hits:
                return "(nothing relevant found in long-term memory)"
            if plugin.summarize_recall:
                try:
                    return await _summarize(hits, query, plugin._resolve_model(ctx))
                except Exception:
                    # Fail-open: fall back to raw hits — degraded output, not
                    # a failed tool.
                    logger.warning(
                        "memory: recall summary failed; returning raw hits",
                        exc_info=True,
                    )
            return "\n\n".join(f"- {_hit_line(h)}" for h in hits)

        return recall

    # -- end-of-run curation -------------------------------------------------

    def _make_hooks(self, index: "Index | None") -> AgentHooks:
        hooks = AgentHooks()
        finalized = False

        @hooks.on(RunCompleted)
        async def _on_completed(ev: RunCompleted, ctx: RunContext[Any]) -> None:
            # The live RunContext carries session_id/run_id and the active
            # agent, none of which are on the event or RunResult.
            nonlocal finalized
            # Guard against any double-dispatch; RunCompleted is once per run.
            if finalized:
                return
            finalized = True
            await self._curate(index, ev.result.entries, ctx)

        return hooks

    async def _curate(
        self,
        index: "Index | None",
        entries: list[TranscriptEntry],
        ctx: RunContext[Any],
    ) -> None:
        """Post-run memory upkeep. Best-effort throughout: the run already
        completed, so every failure here is a WARNING, never an error."""
        digest: _RunDigest | None = None
        if self.auto_curate:
            try:
                current = _format_facts(await self._notes_store().load())
                digest = await _digest(entries, current, self._resolve_model(ctx))
            except Exception:
                logger.warning("memory: run digest failed", exc_info=True)

        if index is not None:
            try:
                await index.add(self._run_docs(entries, ctx, digest))
            except Exception:
                logger.warning("memory: archive ingest failed", exc_info=True)

        if digest and digest.facts:
            try:
                await self._add_facts(digest.facts)
                await self._consolidate_if_over_budget(ctx)
            except Exception:
                logger.warning("memory: notes curation failed", exc_info=True)

    def _run_docs(
        self,
        entries: list[TranscriptEntry],
        ctx: RunContext[Any],
        digest: "_RunDigest | None",
    ) -> list[Doc]:
        """This run's archive docs: its messages, plus the digest summary.

        Ids are deterministic (``run_id:seq``): a resumed run re-ingesting the
        same messages upserts them in place, and messages appended by the
        resume get fresh sequence numbers. Mirrors ``Session.append``.
        """
        rid = ctx.run_id or uuid.uuid4().hex
        now = time.time()
        meta = {"run_id": rid}
        if ctx.session_id:
            meta["session_id"] = ctx.session_id
        docs = [
            Doc(
                id=f"{rid}:{seq}",
                text=text,
                when=now,
                meta={**meta, "kind": "message"},
            )
            for seq, (_, text) in enumerate(_message_texts(entries))
        ]
        if digest and digest.summary.strip():
            docs.append(
                Doc(
                    id=f"{rid}:summary",
                    text=digest.summary.strip(),
                    when=now,
                    meta={**meta, "kind": "summary"},
                )
            )
        return docs

    async def _consolidate_if_over_budget(self, ctx: RunContext[Any]) -> None:
        notes = self._notes_store()
        # Hold the lock across the model call: consolidation rewrites the whole
        # list, so a remember/forget landing between read and save would be
        # silently overwritten. The contention is fine — consolidation is rare
        # (budget breach at run end) and a blocked tool just waits it out.
        async with self._notes_lock:
            body = _format_facts(await notes.load())
            if len(body) <= self.notes_budget:
                return
            facts = await _consolidate(
                body, self.notes_budget, self._resolve_model(ctx)
            )
            if facts:
                await notes.save(facts)


__all__ = [
    "FileNotesStore",
    "Memory",
    "NotesStore",
]
