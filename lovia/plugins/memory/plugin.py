"""Memory plugin: tiered, cross-session memory for an agent.

``Memory`` gives an agent a long-term memory that survives across runs and
sessions, built from two tiers and three verbs the model already understands:

* **Notes** (the *hot* tier) — a tiny, char-budgeted list of date-stamped
  facts that is **always injected** into the system prompt: the user's stable
  preferences, durable facts, important context. Each session stays pinned to
  the Notes it started with (``notes_refresh_after``), so writes never bust
  the provider's cached prompt prefix mid-conversation. The model curates it with
  ``remember(fact)`` / ``forget(fact)``, and (when ``auto_curate``) the plugin
  promotes durable facts into it — and retires disproved ones — automatically
  at the end of each run. A periodic *dream* pass (:meth:`Memory.dream`)
  merges near-duplicates, resolves contradictions (newer wins), and prunes
  stale entries, so Notes stay small and current instead of only growing.
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

Memory can also be split by *scope* (``scope=``): a function maps each run to
a key — a project, say — and that key gets its own Notes and archive next to
the user's. Facts about the user stay visible everywhere; facts about the
project, and its past conversations, stay with the project.

Backends are long-lived and shared by every run (held on the plugin, never
rebuilt per run, never closed by the plugin); :meth:`Memory.setup` only
assembles the per-run tools, instructions, and the ``RunCompleted`` hook.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
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
_DEFAULT_NOTES_BUDGET = 8000
_DEFAULT_DREAM_EVERY_DAYS = 3.0
_DEFAULT_NOTES_REFRESH_AFTER = 300.0
_DREAMED_FILENAME = ".dreamed"
_BACKUP_FILENAME = "MEMORY.md.bak"
_SCOPES_DIRNAME = "scopes"
_SCOPE_META_FILENAME = "scope.json"
_USER = "user"


# ---------------------------------------------------------------------------
# Notes policy helpers — the canonical hot-tier form is a list of short facts,
# persisted as a ``- fact`` bullet list (markdown-native, model-friendly, and
# human-editable). All of this is plugin policy shared by every NotesStore.
# ---------------------------------------------------------------------------


_DATE_PREFIX = re.compile(r"^\[\d{4}-\d{2}\]\s*")


def _normalize_fact(fact: str) -> str:
    """Collapse a fact to a single trimmed line (notes are one fact per line)."""
    return " ".join(fact.split())


def _strip_date(fact: str) -> str:
    """The fact without its ``[YYYY-MM]`` prefix (if any)."""
    return _DATE_PREFIX.sub("", fact)


def _fact_key(fact: str) -> str:
    """Comparison key for dedup and matching: date-stripped, normalized, lowered.

    Keyed comparison is what makes date stamps free: a replayed digest or a
    hand-pasted note never duplicates an existing fact just because the
    month moved on.
    """
    return _normalize_fact(_strip_date(fact)).lower()


def _current_month() -> str:
    """Today as ``YYYY-MM`` — the one seam tests freeze for date-stable asserts."""
    return time.strftime("%Y-%m")


def _stamp(fact: str) -> str:
    """Prefix ``fact`` with the current ``[YYYY-MM]`` unless it carries one."""
    if _DATE_PREFIX.match(fact):
        return fact
    return f"[{_current_month()}] {fact}"


_DATE_SUFFIX = re.compile(r"\s*\[(\d{4}-\d{2})\]\s*$")


def _relocate_stamp(fact: str) -> str:
    """Move a trailing ``[YYYY-MM]`` to the front when no leading one exists.

    Models asked to date their lines sometimes append the stamp instead
    (observed with undated legacy input); the leading form is the one every
    parser, key, and reader relies on.
    """
    if _DATE_PREFIX.match(fact):
        return fact
    m = _DATE_SUFFIX.search(fact)
    if not m:
        return fact
    return f"[{m.group(1)}] {fact[: m.start()].rstrip()}"


def _shed_stamps(fact: str) -> str:
    """Normalize with any leading *and* trailing stamp removed.

    The digest path wants bare facts — dates there are model echoes, wherever
    they landed. The dream path uses :func:`_relocate_stamp` instead, since
    its dates carry meaning and must be kept, just in the leading position.
    """
    return _normalize_fact(_DATE_SUFFIX.sub("", _strip_date(fact)))


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


def _match(facts: list[str], target: str) -> tuple[int, int] | None:
    """``(strength, position)`` of the best match for ``target``, if any.

    Strength ranks the tiers, lower is better: 0 exact, 1 keyed, 2 substring.
    All fuzzy tiers compare via :func:`_fact_key`, so a date prefix on either
    side never blocks a match.
    """
    if target in facts:
        return 0, facts.index(target)
    key = _fact_key(target)
    if not key:
        return None
    for i, f in enumerate(facts):
        if _fact_key(f) == key:
            return 1, i
    for i, f in enumerate(facts):
        fk = _fact_key(f)
        if fk and (key in fk or fk in key):
            return 2, i
    return None


def _drop_fact(facts: list[str], target: str) -> list[str]:
    """Remove the best match for ``target`` (exact → keyed → substring)."""
    m = _match(facts, target)
    return list(facts) if m is None else facts[: m[1]] + facts[m[1] + 1 :]


def _best_match(lists: Sequence[list[str]], target: str) -> tuple[int, int] | None:
    """``(list, position)`` of the best match for ``target`` across ``lists``.

    The strongest match wins, so a substring hit in one tier never takes a
    fact that another tier holds verbatim; a tie goes to the later list (the
    scope tier, which callers pass last — the narrower guess).
    """
    best: tuple[int, int, int] | None = None
    for which, facts in enumerate(lists):
        m = _match(facts, target)
        if m is not None and (best is None or m[0] <= best[0]):
            best = (m[0], which, m[1])
    return None if best is None else (best[1], best[2])


def _merge_new(facts: list[str], new: list[str], elsewhere: Sequence[str] = ()) -> int:
    """Append the facts from ``new`` not already in ``facts``, stamped; in place.

    The shared write policy: normalize, drop blanks and keyed duplicates,
    date-stamp anything undated. Facts in ``elsewhere`` (the other tier's)
    count as present too. Returns the number appended.
    """
    seen = {_fact_key(f) for f in (*facts, *elsewhere)}
    added = 0
    for fact in new:
        norm = _normalize_fact(fact)
        key = _fact_key(norm)
        if not key or key in seen:
            continue
        facts.append(_stamp(norm))
        seen.add(key)
        added += 1
    return added


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
# Tiers: one Notes store + one archive + dream state, under one directory
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class _Tier:
    """One layer of memory: the user's (``key=None``), or one scope's.

    Long-lived and shared by every run, like the stores it holds.
    :meth:`Memory.clear_scope` deletes a scope's files and swaps in fresh
    stores under ``lock``, so code never captures ``notes`` / ``index``:
    every write reads them while holding ``lock`` (reads may race a clear
    and fail; writes must not, or they'd land on deleted stores).
    """

    root: Path
    notes: NotesStore
    index: "Index | None"
    key: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serializes this tier's Notes read-modify-write cycles (tools,
    curation, dream). Code holding two takes the user tier's first."""


def _scope_dirname(key: str) -> str:
    """A scope's directory name: its key's last segment, slugged, plus a hash.

    Any string is a valid key (``/srv/app``, ``ssh://box/x``); the slug keeps
    the directory recognizable and the hash keeps it unique.
    """
    tail = re.split(r"[/\\:]+", key.rstrip("/\\:"))[-1]
    slug = re.sub(r"[^\w.-]+", "-", tail).strip("-.")[:40].rstrip("-.") or "scope"
    return f"{slug}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"


def _default_index(root: Path, embedder: "Embedder | None") -> Index:
    """The default archive under ``root``: keyword, or keyword|vector hybrid."""
    keyword = KeywordIndex(root / _ARCHIVE_FILENAME)
    if embedder is None:
        return keyword
    return keyword | VectorIndex(root / _VECTORS_FILENAME, embedder)


@contextlib.asynccontextmanager
async def _locked(tiers: Sequence[_Tier]) -> AsyncIterator[None]:
    """Hold every tier's lock; callers list the user tier first."""
    async with contextlib.AsyncExitStack() as stack:
        for tier in tiers:
            await stack.enter_async_context(tier.lock)
        yield


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
            "New durable facts worth keeping long-term: stable preferences, "
            "standing rules, corrections, lasting details about the user or "
            "their projects — taken from what the user said or confirmed, "
            "never from the assistant's own statements. Most runs have none "
            "— prefer empty over marginal."
        ),
    )
    stale: list[str] = Field(
        default_factory=list,
        description=(
            "Existing notes this conversation revealed to be wrong, obsolete, "
            "or superseded — quoted closely enough to match. Usually empty."
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

    def touches_notes(self) -> bool:
        return bool(self.facts or self.stale)


class _ScopedRunDigest(_RunDigest):
    """The digest of a scoped run: user facts and scope facts kept apart.

    A subclass rather than an optional field, so an unscoped run's output
    schema — and its prompt — stay exactly what they were.
    """

    facts: list[str] = Field(
        default_factory=list,
        description=(
            "New durable facts about the user that hold wherever they work: "
            "stable preferences, standing rules, corrections, lasting details "
            "about them — taken from what the user said or confirmed, never "
            "from the assistant's own statements. Most runs have none — "
            "prefer empty over marginal."
        ),
    )
    scope_facts: list[str] = Field(
        default_factory=list,
        description=(
            "New durable facts about the scope this conversation belongs to "
            "(typically its project): its code, conventions, decisions, the "
            "lasting state of the work — taken from what the user said or "
            "confirmed. When unsure whether a fact is about the user or the "
            "scope, put it here. Most runs have none — prefer empty over "
            "marginal."
        ),
    )

    def touches_notes(self) -> bool:
        return super().touches_notes() or bool(self.scope_facts)


class _DreamedNotes(BaseModel):
    facts: list[str] = Field(
        default_factory=list,
        description="The full rewritten notes: merged, tidied, dated lines.",
    )


class _ExpandedQuery(BaseModel):
    terms: list[str] = Field(
        default_factory=list,
        description="Short search terms: synonyms, rephrasings, translations.",
    )


# The digest prompt is assembled from shared pieces: an unscoped run gets the
# original three-part prompt byte for byte, a scoped run a four-part one that
# splits facts by whom they are about.
_DIGEST_FACT_RULES = (
    "Only the user is a source: take facts from what the user said "
    "or explicitly confirmed. The assistant's turns are context for reading "
    "the user's — its explanations, findings, and conclusions (including "
    "whatever it drew from tool output) can be wrong and never become facts "
    'on their own; a bare "ok" or "go on" confirms nothing. When the user '
    "corrects or confirms something, the fact is exactly what they said — "
    "never fold in the assistant's explanation around it. Record the "
    'lasting takeaway, not the event ("user prefers X", not "user did X '
    "today\"). The test: would this change the agent's behavior "
    "in a fresh session a month from now? Never record: state re-derivable "
    "from files or a quick command (directory listings, config values, "
    "versions); one-off task outputs and their details (logs, counts, sizes); "
    "open questions or things to verify later; permissions granted for the "
    "task at hand; sensitive personal details (health, relationships, "
    "finances) unless the user explicitly asked to remember them. Most "
    "conversations yield nothing — prefer an empty list over a marginal fact, "
    "and more than three facts is almost never right. Each fact is one short, "
    "self-contained line in the conversation's dominant language."
)

_DIGEST_STALE = (
    "existing notes that this conversation showed to be wrong, "
    "obsolete, or superseded by a new fact — quoted from the current notes "
    "closely enough to identify them. Empty otherwise."
)

_DIGEST_SUMMARY = (
    "a few sentences capturing what the conversation was about — "
    "topics, decisions, and outcomes — written to be found and understood on "
    "its own later, in the conversation's dominant language. Quote names, "
    "numbers, and identifiers exactly as they appeared: detail belongs here, "
    "not in facts. Empty if the conversation is trivial (greetings, tests, "
    "nothing worth recalling)."
)

_DIGEST_INSTRUCTIONS = (
    "You curate an agent's long-term memory. From a conversation, produce "
    "three things.\n"
    "1. facts: new durable facts that will still matter in future, unrelated "
    "sessions — the user's stable preferences and standing rules, corrections "
    "they made, and lasting details about them, their environment, or their "
    f"projects. {_DIGEST_FACT_RULES}\n"
    f"2. stale: {_DIGEST_STALE}\n"
    f"3. summary: {_DIGEST_SUMMARY}"
)


def _scoped_digest_instructions(name: str) -> str:
    """The digest prompt for a run in a scope called ``name`` ("project")."""
    return (
        "You curate an agent's long-term memory, which has two layers: notes "
        f"about the user, shown in every {name}, and notes about the current "
        f"{name}, shown only there. From a conversation, produce four things.\n"
        "1. facts: new durable facts about the user that will still matter in "
        "future, unrelated sessions — their stable preferences and standing "
        "rules, corrections they made, and lasting details about them or "
        "their environment.\n"
        f"2. scope_facts: new durable facts about this {name} — its code, "
        "conventions, and decisions, and the lasting state of the work on it.\n"
        "Which list: a fact about the person — how they like to work, a rule "
        "they hold everywhere, their lasting circumstances — goes in facts; a "
        f"fact about this {name} or this work goes in scope_facts. When "
        f"unsure, choose scope_facts: a {name} fact made global leaks into "
        f"every other {name}, while a preference kept here only has to be "
        "said again elsewhere.\n"
        f"For both lists: {_DIGEST_FACT_RULES}\n"
        f"3. stale: {_DIGEST_STALE} Either layer may hold them.\n"
        f"4. summary: {_DIGEST_SUMMARY}"
    )


_DREAM_INSTRUCTIONS = (
    "You maintain an agent's long-term notes while it sleeps. A note is one "
    "fact on one line that BEGINS with its [YYYY-MM] stamp — every line you "
    "return starts with a stamp: keep an existing one, stamp today's month "
    "on undated lines you keep, and give a merged note the newest of its "
    "dates.\n"
    "Merge only restatements of the same fact, or fragments about the same "
    "subject. Never bundle distinct facts that merely share a category — a "
    "numbered list inside a note means you merged too much. Split any note "
    "that bundles several facts into separate lines. When two notes "
    "conflict, keep the newer one.\n"
    "Drop: one-off events and task logs; deliverables the agent produced "
    "for the user (a plan, a report, a schedule — the archive keeps their "
    "content); state better re-derived than remembered (what a directory "
    "contains, what got installed with its file counts and sizes, config "
    "values, API signatures); permissions that were scoped to a single "
    "task; incidental personal observations the user never asked to keep "
    "(health, mood); open questions and unconfirmed leads; and notes that "
    "have plainly expired. Dropping beats merging: a line that qualifies "
    "for dropping is dropped, never folded into a surviving note. After "
    "splitting a bundled note, judge each piece against this list on its "
    "own. Not every user-related fact is a protected preference: profile "
    "trivia, past reflections, and plans the user once discussed are "
    "droppable when they match the list.\n"
    "Rewrite the survivors at the right altitude: the lasting conclusion, "
    "not the play-by-play. Never invent a fact that is not in the notes, "
    "and never drop the user's explicit rules and preferences, however old. "
    "Keep each note short, self-contained, and in its original language. "
    "Return the full rewritten list, fitting the requested budget."
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
    model: "str | Provider",
) -> _RunDigest:
    return await _run_digest(entries, current_notes, model, None)


async def _scoped_digest(
    entries: list[TranscriptEntry],
    current_notes: str,
    model: "str | Provider",
    scope_name: str,
) -> _ScopedRunDigest:
    """The digest of a run in a scope: the two-layer prompt and output."""
    digest = await _run_digest(entries, current_notes, model, scope_name)
    return cast(_ScopedRunDigest, digest)


async def _run_digest(
    entries: list[TranscriptEntry],
    current_notes: str,
    model: "str | Provider",
    scope_name: str | None,
) -> _RunDigest:
    from ...agent import Agent
    from ...providers import ModelSettings
    from ...runner import Runner

    convo = _render_transcript(entries)
    if not convo.strip():
        return _RunDigest() if scope_name is None else _ScopedRunDigest()
    agent: Agent[Any] = Agent(
        name="memory-digest",
        model=model,
        instructions=(
            _DIGEST_INSTRUCTIONS
            if scope_name is None
            else _scoped_digest_instructions(scope_name)
        ),
        output_type=_RunDigest if scope_name is None else _ScopedRunDigest,
        settings=ModelSettings(temperature=0),
    )
    lists = "facts" if scope_name is None else "facts or scope_facts"
    prompt = (
        f"## Current notes (the source for stale; do NOT repeat them in {lists})\n"
        f"{current_notes or '(empty)'}\n\n"
        f"## Conversation\n{convo}"
    )
    result = await Runner.run(agent, prompt)
    digest = cast(_RunDigest, result.output)
    # Stamping is the code's job: a model echoing a date it saw in the
    # current notes would mislabel a *new* fact as old and skew the dream's
    # newer-wins. Stale entries keep their quotes — matching strips anyway.
    digest.facts = [n for f in digest.facts if (n := _shed_stamps(f))]
    digest.stale = [n for f in digest.stale if (n := _normalize_fact(f))]
    if isinstance(digest, _ScopedRunDigest):
        digest.scope_facts = [n for f in digest.scope_facts if (n := _shed_stamps(f))]
    return digest


async def _dream(
    body: str,
    max_chars: int,
    model: "str | Provider",
) -> list[str]:
    from ...agent import Agent
    from ...providers import ModelSettings
    from ...runner import Runner

    agent: Agent[Any] = Agent(
        name="memory-dream",
        model=model,
        instructions=_DREAM_INSTRUCTIONS,
        output_type=_DreamedNotes,
        settings=ModelSettings(temperature=0),
    )
    # Today's date anchors newer-wins and lets undated legacy lines get
    # stamped; this is a one-shot side-query, so no prompt-cache concerns.
    prompt = (
        f"Today is {_current_month()}. Rewrite these notes to fit "
        f"within roughly {max_chars} characters total.\n\n{body}"
    )
    result = await Runner.run(agent, prompt)
    facts = getattr(result.output, "facts", []) or []
    return [_relocate_stamp(n) for f in facts if (n := _normalize_fact(f))]


async def _expand(
    query: str,
    model: "str | Provider",
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
    model: "str | Provider",
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
    "about the user or project that the user stated or confirmed — not "
    "transient task state, and not your own explanations or conclusions. "
    "Pass one concise, self-contained fact per call."
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

_REMEMBER_SCOPED_DESCRIPTION = (
    "Save a durable fact to long-term memory so you can recall it in future "
    "sessions. Use it for stable preferences, corrections, or lasting details "
    "that the user stated or confirmed — not transient task state, and not "
    "your own explanations or conclusions. Pass one concise, self-contained "
    'fact per call, and say whom it is about: "user" for the person (kept '
    'in every {name}) or "{name}" for this {name} (kept only here).'
)

_RECALL_SCOPED_DESCRIPTION = (
    "Search long-term memory (the archive of this {name}'s past "
    "conversations) for anything relevant to a query. Use it when the user "
    "refers to an earlier conversation, or you need context beyond your "
    "current notes."
)


def _build_instructions(has_index: bool, scope_name: str | None = None) -> str:
    """The Memory guidance; ``scope_name`` set means a scoped run."""
    if scope_name is None:
        parts = [
            "## Memory",
            "You have long-term memory that persists across sessions.",
            "- Your durable NOTES are shown below and are always in context — "
            "they hold the user's stable preferences, facts about them, and "
            "context worth keeping. They are a snapshot from when this "
            "conversation began: anything remembered or forgotten since shows "
            "in the conversation, not below.",
            "- Call `remember(fact)` to save a new durable fact (a preference, a "
            "correction, a stable detail). Keep each fact short and self-contained.",
            "- Call `forget(fact)` to remove a note that is wrong or no longer true.",
        ]
        recall = "past conversations"
    else:
        name = scope_name
        parts = [
            "## Memory",
            "You have long-term memory that persists across sessions, in two layers.",
            "- USER NOTES hold what is true of the user wherever they work: "
            f"their preferences, standing rules, and lasting circumstances. "
            f"{name.upper()} NOTES hold what is true of this {name} only: its "
            "code, conventions, decisions, and the state of the work. Both are "
            "shown below and are always in context. They are a snapshot from "
            "when this conversation began: anything remembered or forgotten "
            "since shows in the conversation, not below.",
            "- Call `remember(fact, about)` to save a new durable fact (a "
            'preference, a correction, a stable detail): about="user" for a '
            f'fact about the person, about="{name}" for a fact about this '
            f'{name} or the work on it — when unsure, "{name}". Keep each fact '
            "short and self-contained.",
            "- Call `forget(fact)` to remove a note that is wrong or no longer "
            "true, from either layer.",
        ]
        recall = f"this {name}'s past conversations"
    if has_index:
        parts.append(
            f"- Call `recall(query)` to search {recall} when the user "
            "refers to something earlier or you need context not in your notes."
        )
    parts.append(
        "When the user asks you to remember something, or corrects a wrong "
        "assumption, call `remember` immediately; everything else is curated "
        "automatically when the run ends — never record transient task details. "
        "Record only what the user said or confirmed, never your own "
        "explanations or conclusions; if a note turns out to be wrong, "
        "`forget` it."
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
    * ``scope=`` — split memory by a per-run key (a project): the key gets
      its own Notes and archive under ``root/scopes/``, next to the user's.

    :meth:`remember` and :meth:`forget` are also public methods, so code can
    seed or clean Notes without a model in the loop. Every public method that
    reads or writes one layer takes ``scope=``: ``None`` (default) is the
    user's layer, a key is that scope's.
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

    scope: "Callable[[RunContext[Any]], str | None] | None" = None
    """Maps a run to its scope key (a project path, say), or ``None`` for a
    run outside any scope. Called once per run, when the run starts.

    A run in a scope sees two sections of Notes — the user's and the
    scope's, each with its own ``notes_budget`` — and its ``remember`` tool
    takes ``about`` (``"user"`` or ``scope_name``); ``forget`` looks in both;
    ``recall`` and the archive cover only the scope's own conversations; the
    end-of-run digest sorts facts into the two layers; each layer dreams on
    its own cadence. ``None`` (default) keeps one layer, exactly as without
    scopes. Scopes live in the default stores under ``root/scopes/`` (one
    directory per key, named from the key plus a hash), so ``scope=`` can't
    be combined with ``notes=`` or a custom ``index=``; ``index=None`` and
    ``embedder=`` apply to every layer."""

    scope_name: str = "project"
    """What the prompts call a scope, and the value of ``remember``'s
    ``about`` for it: one word, not ``"user"``."""

    auto_curate: bool = True
    """When ``True`` (default), one model call at run end promotes durable
    facts into Notes (dropping notes the run disproved) and writes an episode
    summary into the archive. ``False`` = purely manual memory. The dream
    pass is independent of this flag."""

    curate_in_background: bool = False
    """Run the end-of-run curation as a background task instead of inline.

    Inline (default) suits scripts and tests: when ``Runner.run`` returns,
    memory is settled. A long-lived host (the bundled web server does this)
    turns it on so the run's final event isn't held back by curation's model
    calls; :meth:`drain` awaits whatever is still in flight. Curation is
    best-effort in both modes — failures log a warning, never raise — but a
    process that exits without draining may lose the last run's curation."""

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
    """Char budget for Notes; overflowing it triggers an immediate dream and
    is what the meter in the prompt reports."""

    dream_every_days: float | None = _DEFAULT_DREAM_EVERY_DAYS
    """Cadence for the periodic dream — the maintenance pass that merges
    near-duplicates, resolves contradictions (newer wins, by date stamp), and
    drops stale or event-like notes. Between cadences a budget overflow still
    dreams immediately; ``None`` keeps only that overflow trigger. Cadence
    state is the mtime of a ``.dreamed`` marker file under ``root``."""

    notes_refresh_after: float = _DEFAULT_NOTES_REFRESH_AFTER
    """Seconds a session keeps the Notes it started with.

    Notes ride the system prompt, so changing them mid-session would bust the
    provider's cached prefix — the whole conversation history. Each session
    therefore stays pinned to one Notes snapshot (facts saved meanwhile are
    already in its conversation) until it sits idle this long; the next run
    then re-reads Notes, by which time the provider cache has expired anyway.
    Match it to your provider's cache lifetime (Anthropic's default is five
    minutes); ``0`` re-reads Notes on every run. Snapshots live in process
    memory, so a restart simply re-reads."""

    model: "str | Provider | None" = None
    """Model for the curation side-queries. ``None`` (default) reuses the
    host agent's model."""

    name: str = "memory"
    """Plugin name."""

    def __post_init__(self) -> None:
        if self.notes_refresh_after < 0:
            raise UserError(
                f"Memory: notes_refresh_after must be >= 0, got {self.notes_refresh_after}",
                hint="use 0 to re-read Notes on every run",
            )
        if not re.fullmatch(r"\w+", self.scope_name) or self.scope_name == _USER:
            raise UserError(
                f"Memory: scope_name must be one word other than 'user', got "
                f"{self.scope_name!r}",
                hint="it names the scope in the prompts and in remember's "
                "`about` argument, next to 'user' — e.g. 'project'",
            )
        if self.scope is not None:
            custom = [
                arg
                for arg, given in (
                    ("notes=", self.notes is not None),
                    ("index=", not isinstance(self.index, EllipsisType | None)),
                )
                if given
            ]
            if custom:
                raise UserError(
                    f"Memory: scope= can't be combined with {' or '.join(custom)}",
                    hint=(
                        "every scope keeps its Notes and archive in the default "
                        "stores under root/scopes/; drop the custom store, or "
                        "leave scope unset (index=None and embedder= are fine)"
                    ),
                )
        if self.notes is None:
            self.notes = FileNotesStore(Path(self.root) / _NOTES_FILENAME)
        if isinstance(self.index, EllipsisType):
            self.index = _default_index(Path(self.root), self.embedder)
            self._lexical_only = self.embedder is None
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
        self._user = _Tier(Path(self.root), self.notes, self.index)
        # Scope key -> its tier, created on first use and kept for good.
        self._scopes: dict[str, _Tier] = {}
        # Strong refs to background curation tasks (curate_in_background):
        # without them the event loop may garbage-collect a task mid-flight.
        self._curation_tasks: set[asyncio.Task[None]] = set()
        # Pinned Notes blocks -> (block, last used, monotonic seconds). Keyed
        # by session id, or (session id, scope key) in a scoped run, so a
        # session that moves to another scope never keeps the old one's notes.
        self._snapshots: dict[str | tuple[str, str], tuple[str, float]] = {}
        # Bumped by deliberate edits (replace_notes, clear_scope): a snapshot
        # rendered under an older epoch is never (re)pinned over them.
        self._notes_epoch = 0

    def _resolve_model(self, ctx: RunContext[Any]) -> "str | Provider":
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

    # -- tiers ------------------------------------------------------------------
    # Internal helpers that act on one tier take ``tier=None`` for the user's.

    def _tier(self, scope: str | None) -> _Tier:
        """The tier a public method's ``scope=`` names."""
        if scope is None:
            return self._user
        if self.scope is None:
            raise UserError(
                f"Memory: scope={scope!r} names a scope, but this Memory has none",
                hint="construct Memory(scope=...) to split memory by scope",
            )
        return self._scope_tier(scope)

    def _scope_tier(self, key: str) -> _Tier:
        tier = self._scopes.get(key)
        if tier is None:
            root = Path(self.root) / _SCOPES_DIRNAME / _scope_dirname(key)
            notes, index = self._scope_stores(root, key)
            tier = self._scopes[key] = _Tier(root, notes, index, key)
        return tier

    def _scope_stores(self, root: Path, key: str) -> tuple[NotesStore, "Index | None"]:
        """Open a scope's default stores, recording its key beside them —
        the directory name alone can't be read back into the key."""
        root.mkdir(parents=True, exist_ok=True)
        (root / _SCOPE_META_FILENAME).write_text(
            json.dumps({"key": key}, ensure_ascii=False), encoding="utf-8"
        )
        index = (
            None if self._user.index is None else _default_index(root, self.embedder)
        )
        return FileNotesStore(root / _NOTES_FILENAME), index

    # -- notes policy (shared by tools and curation) --------------------------

    def _notes_store(self) -> NotesStore:
        return self._user.notes

    async def _render_tier(self, tier: _Tier, label: str, empty: str) -> str:
        body = _format_facts(await tier.notes.load())
        return f"{label} {_meter(len(body), self.notes_budget)}\n{body or empty}"

    async def _render_notes(self) -> str:
        """The Notes block of an unscoped run."""
        return await self._render_tier(
            self._user, "NOTES", "(empty — use `remember` to save durable facts)"
        )

    async def _render_scoped(self, scope: _Tier) -> str:
        """The Notes block of a scoped run: the user's section, then the scope's."""
        user = await self._render_tier(self._user, "USER NOTES", "(empty)")
        mine = await self._render_tier(
            scope, f"{self.scope_name.upper()} NOTES", "(empty)"
        )
        return f"{user}\n\n{mine}"

    async def _render(self, scope: _Tier | None) -> str:
        return await (
            self._render_notes() if scope is None else self._render_scoped(scope)
        )

    def _snapshot_key(
        self, session_id: str, scope: _Tier | None
    ) -> str | tuple[str, str]:
        return session_id if scope is None else (session_id, cast(str, scope.key))

    async def _session_notes(
        self, session_id: str | None, scope: _Tier | None = None
    ) -> str:
        """The Notes block for a run: the session's snapshot while it stays
        warm, else a fresh render (which the caller pins via :meth:`_pin`)."""
        if session_id is None or self.notes_refresh_after == 0:
            # No session means no cross-run prefix to keep stable.
            return await self._render(scope)
        now = time.monotonic()
        # A snapshot idle past the window is useless — the provider cache is
        # cold by now too — so expiry doubles as the eviction rule.
        self._snapshots = {
            sid: snap
            for sid, snap in self._snapshots.items()
            if now - snap[1] < self.notes_refresh_after
        }
        pinned = self._snapshots.get(self._snapshot_key(session_id, scope))
        return pinned[0] if pinned else await self._render(scope)

    def _pin(
        self, session_id: str | None, text: str, epoch: int, scope: _Tier | None = None
    ) -> None:
        """Pin ``text`` as the session's snapshot and restart its idle clock —
        unless a deliberate edit superseded it since ``epoch``."""
        if session_id is None or self.notes_refresh_after == 0:
            return
        if epoch == self._notes_epoch:
            key = self._snapshot_key(session_id, scope)
            self._snapshots[key] = (text, time.monotonic())

    def _unpin_all(self) -> None:
        """After a deliberate edit: every session's next run re-reads Notes
        (at the price of one cache miss each)."""
        self._notes_epoch += 1
        self._snapshots.clear()

    async def _add_facts(self, new: list[str], tier: _Tier | None = None) -> int:
        """Merge normalized facts into Notes (date-stamped, keyed dedup)."""
        tier = tier or self._user
        async with tier.lock:
            facts = await tier.notes.load()
            added = _merge_new(facts, new)
            if added:
                await tier.notes.save(facts)
        return added

    async def _drop(self, fact: str, tiers: Sequence[_Tier]) -> bool:
        """Remove the best match for ``fact`` from whichever tier holds it."""
        norm = _normalize_fact(fact)
        if not norm:
            return False
        async with _locked(tiers):
            lists = [await t.notes.load() for t in tiers]
            hit = _best_match(lists, norm)
            if hit is None:
                return False
            which, pos = hit
            del lists[which][pos]
            await tiers[which].notes.save(lists[which])
        return True

    async def _apply_digest(
        self, digest: _RunDigest, scope: _Tier | None = None
    ) -> tuple[int, int]:
        """Apply a run digest to Notes atomically: drop stale, then add facts.

        One lock cycle, one save per changed tier — a supersession (stale old
        + new fact) can never be torn apart by a concurrent write landing in
        between, even when the two sit in different tiers.
        Returns ``(dropped, added)``.
        """
        tiers = [self._user] if scope is None else [self._user, scope]
        async with _locked(tiers):
            lists = [await t.notes.load() for t in tiers]
            changed = [False] * len(tiers)
            dropped = 0
            for target in digest.stale:
                hit = _best_match(lists, target)
                if hit is None:
                    continue
                which, pos = hit
                logger.info("memory: dropped stale note: %s", lists[which].pop(pos))
                changed[which] = True
                dropped += 1
            # A fact the other tier already holds isn't added again; one that
            # moves tiers arrives as stale (dropped above) plus a new fact.
            added = _merge_new(lists[0], digest.facts, lists[1] if scope else ())
            changed[0] = changed[0] or bool(added)
            if scope is not None and isinstance(digest, _ScopedRunDigest):
                scoped = _merge_new(lists[1], digest.scope_facts, lists[0])
                changed[1] = changed[1] or bool(scoped)
                added += scoped
            for tier, facts, dirty in zip(tiers, lists, changed):
                if dirty:
                    await tier.notes.save(facts)
        return dropped, added

    async def remember(self, fact: str, *, scope: str | None = None) -> bool:
        """Add one durable fact to Notes; ``False`` if it was already there.

        Backs the model's ``remember`` tool; call it directly to seed memories
        programmatically (onboarding data, migrations, an admin UI).
        ``scope`` picks the layer: ``None`` the user's, a key that scope's.
        """
        return await self._add_facts([fact], self._tier(scope)) == 1

    async def forget(self, fact: str, *, scope: str | None = None) -> bool:
        """Remove the best-matching note; ``False`` if nothing matched.

        Matching is exact → case-insensitive → substring, same as the model's
        ``forget`` tool, which this backs. Looks only in the layer ``scope``
        names (the tool, in a scoped run, looks in both).
        """
        return await self._drop(fact, [self._tier(scope)])

    async def notes_body(self, *, scope: str | None = None) -> str:
        """The current Notes as their canonical ``- fact`` body (may be ``""``).

        The read half of the editing seam; :meth:`replace_notes` is the write
        half. The web UI's memory editor is built on the pair.
        """
        return _format_facts(await self._tier(scope).notes.load())

    async def clear_scope(self, scope: str) -> None:
        """Delete everything one scope remembers: its Notes and its archive.

        For "forget this project" flows — Notes can be emptied by hand, the
        archive can't. Irreversible: the scope's directory, dream backup
        included, is removed. A run still going in that scope keeps working
        and starts the scope afresh with whatever it saves from then on.
        """
        tier = self._tier(scope)
        async with tier.lock:
            try:
                with contextlib.suppress(FileNotFoundError):
                    await asyncio.to_thread(shutil.rmtree, tier.root)
            finally:
                # Reopen even after a partial delete: the old index objects
                # cache schema state for files that may no longer exist.
                tier.notes, tier.index = self._scope_stores(tier.root, scope)
                self._unpin_all()

    async def replace_notes(self, body: str, *, scope: str | None = None) -> str:
        """Replace Notes wholesale with the facts parsed from ``body``.

        The bulk counterpart to :meth:`remember` / :meth:`forget`, for editor
        flows (the web UI, imports): ``body`` uses the same ``- fact`` per line
        form :meth:`notes_body` returns. Non-bullet lines are ignored, facts
        are normalized, deduplicated by key, and date-stamped when undated —
        the same policy every other Notes write applies. Returns the
        canonical body stored.

        Unlike model and curation writes, a wholesale replacement is a
        deliberate edit, so it also unpins every session's Notes snapshot:
        each session's next run sees it (at the price of one cache miss).
        ``scope`` picks the layer, as everywhere.
        """
        facts: list[str] = []
        _merge_new(facts, _parse_facts(body))
        tier = self._tier(scope)
        async with tier.lock:
            await tier.notes.save(facts)
            self._unpin_all()
        return _format_facts(facts)

    # -- setup -----------------------------------------------------------------

    async def setup(self, ctx: RunContext[Any]) -> PluginInstance:
        # Resolved once: the whole run — tools, prompt, curation — lands in
        # the scope it started in.
        key = self.scope(ctx) if self.scope is not None else None
        scope = None if key is None else self._scope_tier(key)
        archived = self._user.index is not None

        tools: list[Tool] = [self._make_remember(scope), self._make_forget(scope)]
        if archived:
            tools.append(self._make_recall(scope or self._user))

        sid, epoch = ctx.session_id, self._notes_epoch
        notes = await self._session_notes(sid, scope)
        self._pin(sid, notes, epoch, scope)

        async def repin() -> None:
            # Teardown runs on every outcome. The provider cache stays warm
            # from the run's last call, not its start, so restart the idle
            # clock here — re-pinning the snapshot if another session's sweep
            # evicted it while this run outlasted the window.
            self._pin(sid, notes, epoch, scope)

        guidance = _build_instructions(
            archived, None if scope is None else self.scope_name
        )
        return PluginInstance(
            tools=tools,
            instructions=f"{guidance}\n\n{notes}",
            hooks=self._make_hooks(scope),
            aclose=repin,
        )

    # -- tools -------------------------------------------------------------------

    def _make_remember(self, scope: _Tier | None = None) -> Tool:
        plugin = self
        if scope is None:

            @tool(name="remember", description=_REMEMBER_DESCRIPTION)
            async def remember(
                fact: Annotated[str, "The durable fact to remember."],
            ) -> str:
                if await plugin.remember(fact):
                    return "Remembered. It will be available in future sessions."
                return "Already in your notes — nothing to add."

            return remember

        async def remember_about(fact: str, about: str) -> str:
            tier = plugin._user if about == _USER else scope
            if await plugin._add_facts([fact], tier):
                return "Remembered. It will be available in future sessions."
            return "Already in your notes — nothing to add."

        # The choices for `about` are only known per Memory, so its annotation
        # is set here; @tool reads annotations objects and strings alike.
        about = Literal[(_USER, self.scope_name)]  # type: ignore[valid-type]
        remember_about.__annotations__.update(
            fact=Annotated[str, "The durable fact to remember."],
            about=Annotated[about, "Whom the fact is about."],
        )
        return tool(
            remember_about,
            name="remember",
            description=_REMEMBER_SCOPED_DESCRIPTION.format(name=self.scope_name),
        )

    def _make_forget(self, scope: _Tier | None = None) -> Tool:
        plugin = self
        tiers = [self._user] if scope is None else [self._user, scope]

        @tool(name="forget", description=_FORGET_DESCRIPTION)
        async def forget(
            fact: Annotated[str, "Text matching the note to remove."],
        ) -> str:
            if await plugin._drop(fact, tiers):
                return "Forgotten."
            return "No matching note found to forget."

        return forget

    def _make_recall(self, tier: _Tier) -> Tool:
        plugin = self
        description = (
            _RECALL_DESCRIPTION
            if tier.key is None
            else _RECALL_SCOPED_DESCRIPTION.format(name=self.scope_name)
        )

        @tool(name="recall", description=description)
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
            index = cast(Index, tier.index)  # read now: clear_scope may reopen it
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

    def _make_hooks(self, scope: _Tier | None) -> AgentHooks:
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
            index = self._user.index if scope is None else None
            curation = self._curate(index, ev.result.entries, ctx, scope)
            if self.curate_in_background:
                # Off the hot path: the run's final event shouldn't wait for
                # curation's model calls. _curate never raises (best-effort
                # throughout), so the task can't die loudly.
                task = asyncio.create_task(curation)
                self._curation_tasks.add(task)
                task.add_done_callback(self._curation_tasks.discard)
            else:
                await curation

        return hooks

    async def drain(self) -> None:
        """Wait for in-flight background curation (a no-op when inline)."""
        while self._curation_tasks:
            await asyncio.gather(*tuple(self._curation_tasks), return_exceptions=True)

    async def _curate(
        self,
        index: "Index | None",
        entries: list[TranscriptEntry],
        ctx: RunContext[Any],
        scope: _Tier | None = None,
    ) -> None:
        """Post-run memory upkeep: ``index`` is an unscoped run's archive,
        ``scope`` a scoped run's tier (whose archive is read at write time).
        Best-effort throughout: the run already completed, so every failure
        here is a WARNING, never an error."""
        digest: _RunDigest | None = None
        if self.auto_curate:
            try:
                model = self._resolve_model(ctx)
                current = await self._digest_notes(scope)
                digest = await (
                    _digest(entries, current, model)
                    if scope is None
                    else _scoped_digest(entries, current, model, self.scope_name)
                )
            except Exception:
                logger.warning("memory: run digest failed", exc_info=True)

        try:
            await self._archive(index, self._run_docs(entries, ctx, digest), scope)
        except Exception:
            logger.warning("memory: archive ingest failed", exc_info=True)

        if digest and digest.touches_notes():
            try:
                await self._apply_digest(digest, scope)
            except Exception:
                logger.warning("memory: notes curation failed", exc_info=True)
        # Outside the digest guard on purpose: manual remember/forget traffic
        # and hand edits deserve maintenance too, auto_curate or not. Each
        # tier on its own, so one failing dream doesn't skip the other.
        for tier in (self._user,) if scope is None else (self._user, scope):
            try:
                await self._dream_if_due(ctx, tier)
            except Exception:
                logger.warning("memory: dream failed", exc_info=True)

    @staticmethod
    async def _archive(
        index: "Index | None", docs: list[Doc], scope: _Tier | None
    ) -> None:
        if scope is None:
            if index is not None:
                await index.add(docs)
            return
        # Under the tier's lock, with the index read inside it: clear_scope
        # deletes the files and reopens the stores under the same lock, and
        # curation may have spent seconds in the digest call before getting
        # here — a captured index could be one that no longer has a file.
        async with scope.lock:
            if scope.index is not None:
                await scope.index.add(docs)

    async def _digest_notes(self, scope: _Tier | None) -> str:
        """The current Notes as the digest sees them (the source for stale)."""
        user = _format_facts(await self._user.notes.load())
        if scope is None:
            return user
        mine = _format_facts(await scope.notes.load())
        return (
            f"### About the user\n{user or '(empty)'}\n\n"
            f"### About this {self.scope_name}\n{mine or '(empty)'}"
        )

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

    # -- dreaming: periodic notes maintenance --------------------------------

    async def dream(
        self, model: "str | Provider | None" = None, *, scope: str | None = None
    ) -> tuple[int, int]:
        """Tidy Notes now; returns ``(before, after)`` note counts.

        The dream pass merges near-duplicates into one well-phrased note,
        resolves contradictions (newer wins, by date stamp), drops event-like
        or expired notes, and rewrites the survivors within ``notes_budget``.
        It runs by itself on the ``dream_every_days`` cadence and immediately
        when Notes overflow their budget; call it directly for on-demand
        tidying (a cleanup button, a one-off migration). ``model`` falls back
        to ``Memory(model=...)``; ``scope`` picks the layer, as everywhere.
        """
        resolved = model if model is not None else self.model
        if resolved is None:
            raise UserError(
                "Memory.dream() has no model to run on",
                hint="pass dream(model=...) or construct Memory(model=...)",
            )
        return await self._run_dream(resolved, self._tier(scope))

    async def _dream_if_due(
        self, ctx: RunContext[Any], tier: _Tier | None = None
    ) -> None:
        tier = tier or self._user
        body = _format_facts(await tier.notes.load())
        over = len(body) > self.notes_budget
        if not over and not await asyncio.to_thread(self._dream_due, tier):
            return
        await self._run_dream(self._resolve_model(ctx), tier)

    async def _run_dream(self, model: "str | Provider", tier: _Tier) -> tuple[int, int]:
        # Hold the lock across the model call: the dream rewrites the whole
        # list, so a remember/forget landing between read and save would be
        # silently overwritten. The contention is fine — dreams are rare and
        # a blocked tool just waits one out.
        async with tier.lock:
            facts = await tier.notes.load()
            before = len(facts)
            if not facts:
                await asyncio.to_thread(self._touch_marker, tier)
                return (0, 0)
            body = _format_facts(facts)
            rewritten = await _dream(body, self.notes_budget, model)
            after = before
            if rewritten:
                # One-generation undo for the only wholesale-rewrite path.
                # Deeper history belongs to git and to the archive, which
                # keeps the conversations every note came from.
                await asyncio.to_thread(self._write_backup, tier, body)
                await tier.notes.save(rewritten)
                after = len(rewritten)
            await asyncio.to_thread(self._touch_marker, tier)
        logger.info("memory: dream rewrote notes, %d → %d", before, after)
        return (before, after)

    def dreamed_at(self, *, scope: str | None = None) -> float | None:
        """Epoch mtime of the last dream, or ``None`` before the first one.

        Reads the layer's ``.dreamed`` marker; the web UI shows this next to
        its tidy-up button.
        """
        return self._dreamed_at(self._tier(scope))

    def _dreamed_at(self, tier: _Tier) -> float | None:
        try:
            return (tier.root / _DREAMED_FILENAME).stat().st_mtime
        except FileNotFoundError:
            return None

    def _dream_due(self, tier: _Tier) -> bool:
        """Cadence check: ``dream_every_days`` elapsed and Notes changed since."""
        if self.dream_every_days is None:
            return False
        last = self._dreamed_at(tier)
        if last is None:
            # First contact starts the clock; don't dream over a newborn file.
            self._touch_marker(tier)
            return False
        if time.time() - last < self.dream_every_days * 86400.0:
            return False
        return self._notes_changed_since(tier, last)

    @staticmethod
    def _notes_changed_since(tier: _Tier, when: float) -> bool:
        notes = tier.notes
        if isinstance(notes, FileNotesStore):
            try:
                return notes._path.stat().st_mtime > when
            except FileNotFoundError:
                return False
        # A custom store has no cheap change signal — the cadence alone
        # decides, at worst re-dreaming an unchanged list.
        return True

    def _touch_marker(self, tier: _Tier | None = None) -> None:
        marker = (tier or self._user).root / _DREAMED_FILENAME
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    @staticmethod
    def _write_backup(tier: _Tier, body: str) -> None:
        bak = tier.root / _BACKUP_FILENAME
        bak.parent.mkdir(parents=True, exist_ok=True)
        bak.write_text(body, encoding="utf-8")


__all__ = [
    "FileNotesStore",
    "Memory",
    "NotesStore",
]
