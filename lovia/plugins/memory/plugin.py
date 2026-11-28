"""Memory plugin: tiered, cross-session memory for an agent.

``Memory`` gives an agent a long-term memory that survives across runs and
sessions, built from two tiers and three verbs the model already understands:

* **Notes** (the *hot* tier) — a small, char-budgeted list of date-stamped
  facts that is **always injected** into the system prompt. The agent is
  their only writer: it calls ``remember(fact)`` / ``forget(fact)`` at the
  moment the user says something worth keeping — the one point where the
  full conversation is in view, so a standing rule can be told from a
  one-off. A note that supersedes another replaces it (``replaces=``), and a
  full list refuses new notes until room is made, so Notes stay small and
  current without anything rewriting them behind the agent's back. Each
  session stays pinned to the Notes it started with
  (``notes_refresh_after``), so writes never bust the provider's cached
  prompt prefix mid-conversation.
* **Archive** (the *cold* tier) — a searchable index of past conversations,
  one document per run, pulled in on demand via ``recall(query)``.

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

Memory makes no model call of its own after a run: the end of a run only
indexes it (:class:`~lovia.events.RunCompleted`; ``result.entries`` is the
run's own complete transcript). The document id is the run id, so a resumed
run that re-ingests simply upserts. The model is used only when the agent
asks to ``recall``: the query is expanded with synonyms and translations
before hitting the lexical index (``expand_query="auto"`` turns this off when
a semantic arm is configured), and the hits are summarized for the question.

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
_DEFAULT_NOTES_BUDGET = 4000
_DEFAULT_NOTES_REFRESH_AFTER = 300.0
_SCOPES_DIRNAME = "scopes"
_SCOPE_META_FILENAME = "scope.json"
_USER = "user"
# A run's archive document keeps this many chars: its head (what was asked)
# and its tail (how it ended) survive, the middle of a long run does not.
_RUN_DOC_CHARS = 8000


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

    Keyed comparison is what makes date stamps free: a hand-pasted note never
    duplicates an existing fact just because the month moved on.
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
# Tiers: one Notes store + one archive, under one directory
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
    """Serializes this tier's Notes read-modify-write cycles. Code holding
    two takes the user tier's first."""


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
# Transcript → doc (the only place the cold tier meets lovia's data model)
# ---------------------------------------------------------------------------


def _run_text(entries: list[TranscriptEntry]) -> str:
    """A run as one searchable text: its user and assistant turns, in order.

    One document per run keeps a question next to its answer, so a hit reads
    on its own. Tool calls and results stay out — they are bulk, and what
    mattered in them the assistant said.
    """
    lines: list[str] = []
    for m in entries_to_messages(entries):
        if m.role not in ("user", "assistant"):
            continue
        text = text_of(m.content).strip()
        if text:
            lines.append(f"{m.role.upper()}: {text}")
    text = "\n".join(lines)
    if len(text) <= _RUN_DOC_CHARS:
        return text
    half = _RUN_DOC_CHARS // 2
    return f"{text[:half]}\n[...]\n{text[-half:]}"


def _run_doc(entries: list[TranscriptEntry], ctx: RunContext[Any]) -> Doc | None:
    """This run's archive doc, or ``None`` when it said nothing.

    The id is the run id: a resumed run re-ingests over its earlier copy.
    """
    text = _run_text(entries)
    if not text:
        return None
    rid = ctx.run_id or uuid.uuid4().hex
    meta = {"run_id": rid}
    if ctx.session_id:
        meta["session_id"] = ctx.session_id
    return Doc(id=rid, text=text, when=time.time(), meta=meta)


def _hit_line(hit: Hit) -> str:
    """Render a hit for the model: date-stamped so answers can be grounded in time."""
    if hit.doc.when > 0:
        day = time.strftime("%Y-%m-%d", time.localtime(hit.doc.when))
        return f"[{day}] {hit.doc.text}"
    return hit.doc.text


# ---------------------------------------------------------------------------
# Recall side-queries — tool-less, plugin-less sub-agent runs via
# ``Runner.run``. They dogfood structured output + the provider chain and
# cannot recurse (the sub-agent has no Memory plugin). Runner/Agent are
# imported lazily to avoid an import cycle (plugins -> runner -> loop ->
# plugins.base).
# ---------------------------------------------------------------------------


class _ExpandedQuery(BaseModel):
    terms: list[str] = Field(
        default_factory=list,
        description="Short search terms: synonyms, rephrasings, translations.",
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


# What earns a note. The agent is the only judge, so this is the whole
# admission policy: a closed list of kinds, each with another home named for
# everything that is not one of them.
_WORTH_SAVING = (
    "Save only what the user said or confirmed, and only if it would change "
    "what you do in a later, unrelated conversation:\n"
    "- how the user wants you to work — a correction, a standing preference "
    "or rule;\n"
    "- who the user is — their role and lasting circumstances;\n"
    "- where things are — servers, accounts, tools, and other resources the "
    "user would otherwise have to point you to again;\n"
    "- decisions and constraints of the user's work that its files and "
    "history do not show.\n"
    "Unless the user asks you to remember it, never save: what the files, "
    "their instructions, or version history already record; progress, "
    "results, or details of the task at hand; your own findings and "
    "conclusions; a guess at the user's interests, mood, or skill; a request "
    "the user made for this one occasion; general knowledge that can be "
    "looked up. When unsure, do not save.\n"
    "One short, self-contained line per call. When a note already covers the "
    "subject, pass that note's text as `replaces` instead of adding a second "
    "one."
)

_REMEMBER_DESCRIPTION = (
    f"Save a fact to your notes, which you see in every future session. {_WORTH_SAVING}"
)

_REMEMBER_SCOPED_DESCRIPTION = (
    _REMEMBER_DESCRIPTION + ' Say whom the fact is about: "user" for the '
    'person (kept in every {name}) or "{name}" for this {name} or the work '
    'on it (kept only here) — when unsure, "{name}".'
)

_FACT_DOC = "The fact to remember, as one line."
_REPLACES_DOC = "The full text of the note this fact supersedes, if any."

_FORGET_DESCRIPTION = (
    "Remove a fact from long-term memory when it is wrong or no longer true. "
    "Pass the note's full text, as your notes show it: the closest match goes."
)

_FORGET_SCOPED_DESCRIPTION = (
    _FORGET_DESCRIPTION + " It searches the user's notes and this {name}'s "
    "alike, so a partial text can take a note from the wrong one."
)

_RECALL_DESCRIPTION = (
    "Search long-term memory (the archive of past conversations) for anything "
    "relevant to a query. Use it when the user refers to an earlier "
    "conversation, or you need context beyond your current notes."
)

_RECALL_SCOPED_DESCRIPTION = (
    "Search long-term memory (the archive of this {name}'s past "
    "conversations) for anything relevant to a query. Use it when the user "
    "refers to an earlier conversation, or you need context beyond your "
    "current notes."
)

_Written = Literal["added", "replaced", "duplicate", "no_match", "full"]

_WRITTEN_REPLY: dict[_Written, str] = {
    "added": "Remembered. It will be in your notes in future sessions.",
    "replaced": "Replaced the old note.",
    "duplicate": "Already in your notes — nothing to add.",
    "no_match": (
        "No note matches `replaces` — nothing changed. Pass the old note's "
        "full text, or leave `replaces` empty to add a new note."
    ),
    "full": (
        "Your notes are full — nothing saved. Make room first: pass the note "
        "this one supersedes as `replaces`, or `forget` one that no longer "
        "matters. They hold:"
    ),
}


def _build_instructions(scope_name: str | None = None) -> str:
    """The Memory guidance; ``scope_name`` set means a scoped run."""
    if scope_name is None:
        parts = [
            "## Memory",
            "You have long-term memory that persists across sessions.",
            "- Your NOTES are shown below and are always in context. They are "
            "a snapshot from when this conversation began: anything "
            "remembered or forgotten since shows in the conversation, not "
            "below.",
        ]
    else:
        name = scope_name
        parts = [
            "## Memory",
            "You have long-term memory that persists across sessions, in two layers.",
            "- USER NOTES hold what is true of the user wherever they work. "
            f"{name.upper()} NOTES hold what is true of this {name} only. Both "
            "are shown below and are always in context. They are a snapshot "
            "from when this conversation began: anything remembered or "
            "forgotten since shows in the conversation, not below.",
        ]
    # What makes a fact worth keeping is in `remember`'s description; this
    # says only what a description cannot: the notes below and when to act.
    parts.append(
        "Call `remember` when the user asks you to remember something, "
        "corrects how you work, or tells you something that will still hold "
        "in later, unrelated conversations — nothing else saves it. Most "
        "conversations leave nothing to save. When a note contradicts what "
        "the user says now, trust the user and fix the note (`remember` with "
        "`replaces`, or `forget`)."
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

    The agent is the only one who writes Notes by itself; nothing extracts
    facts from a finished run. :meth:`remember` and :meth:`forget` are also
    public methods, so code can seed or clean Notes without a model in the
    loop — or run its own extraction from a ``RunCompleted`` hook. Every
    public method that reads or writes one layer takes ``scope=``: ``None``
    (default) is the user's layer, a key is that scope's.
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
    ``recall`` and the archive cover only the scope's own conversations.
    ``None`` (default) keeps one layer, exactly as without scopes. Scopes
    live in the default stores under ``root/scopes/`` (one directory per key,
    named from the key plus a hash), so ``scope=`` can't be combined with
    ``notes=`` or a custom ``index=``; ``index=None`` and ``embedder=`` apply
    to every layer."""

    scope_name: str = "project"
    """What the prompts call a scope, and the value of ``remember``'s
    ``about`` for it: one word, not ``"user"``."""

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
    """Char budget for a layer's Notes, shown to the model as a meter. The
    ``remember`` tool refuses a note that would grow a layer past it, so the
    agent makes room (``replaces=``, ``forget``) instead of only adding.
    Writes from code — :meth:`remember`, :meth:`replace_notes` — are not
    held to it."""

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
    """Model for ``recall``'s side-queries (query expansion, hit summary).
    ``None`` (default) reuses the host agent's model."""

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

    # -- notes policy -----------------------------------------------------------

    def _label(self, tier: _Tier, scoped: bool) -> str:
        if not scoped:
            return "NOTES"
        return f"{(_USER if tier.key is None else self.scope_name).upper()} NOTES"

    async def _render_tier(self, tier: _Tier, scoped: bool) -> str:
        body = _format_facts(await tier.notes.load())
        meter = _meter(len(body), self.notes_budget)
        return f"{self._label(tier, scoped)} {meter}\n{body or '(empty)'}"

    async def _render(self, scope: _Tier | None) -> str:
        """The Notes block of a run: the user's section, then the scope's."""
        if scope is None:
            return await self._render_tier(self._user, False)
        user = await self._render_tier(self._user, True)
        return f"{user}\n\n{await self._render_tier(scope, True)}"

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

    async def _write(
        self,
        fact: str,
        into: _Tier,
        *,
        replaces: str = "",
        among: Sequence[_Tier] | None = None,
        budgeted: bool = False,
    ) -> _Written:
        """Write one fact into ``into``, optionally in place of an old note.

        ``among`` are the tiers ``replaces`` may name a note in (user tier
        first; default just ``into``). One lock cycle, so a supersession —
        old note out, new fact in — is never torn apart by a concurrent
        write, even across tiers. ``budgeted`` refuses a write that would
        grow ``into`` past ``notes_budget``; one that shrinks an over-budget
        list is how room gets made, so it passes.
        """
        tiers = list(among or [into])
        if not _fact_key(fact):
            return "duplicate"
        old = _normalize_fact(replaces)
        async with _locked(tiers):
            lists = [await t.notes.load() for t in tiers]
            target = tiers.index(into)
            before = len(_format_facts(lists[target]))
            dirty: set[int] = set()
            if old:
                hit = _best_match(lists, old)
                if hit is None:
                    return "no_match"
                del lists[hit[0]][hit[1]]
                dirty.add(hit[0])
            elsewhere = [f for i, fs in enumerate(lists) if i != target for f in fs]
            if _merge_new(lists[target], [fact], elsewhere):
                dirty.add(target)
            elif not old:
                return "duplicate"
            after = len(_format_facts(lists[target]))
            if budgeted and after > max(self.notes_budget, before):
                return "full"
            for i in sorted(dirty):
                await tiers[i].notes.save(lists[i])
        return "replaced" if old else "added"

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

    async def remember(
        self, fact: str, *, scope: str | None = None, replaces: str = ""
    ) -> bool:
        """Add one durable fact to Notes; ``False`` if nothing changed.

        Backs the model's ``remember`` tool; call it directly to seed
        memories programmatically (onboarding data, migrations, an admin UI,
        your own extraction from a ``RunCompleted`` hook). ``replaces`` names
        an existing note (matched as :meth:`forget` does) that the fact
        takes the place of; nothing changes when no note matches. ``scope``
        picks the layer: ``None`` the user's, a key that scope's. Not held
        to ``notes_budget``.
        """
        written = await self._write(fact, self._tier(scope), replaces=replaces)
        return written in ("added", "replaced")

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
        archive can't. Irreversible: the scope's directory is removed. A run
        still going in that scope keeps working and starts the scope afresh
        with whatever it saves from then on.
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

        Unlike the model's writes, a wholesale replacement is a deliberate
        edit, so it also unpins every session's Notes snapshot: each
        session's next run sees it (at the price of one cache miss).
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
        # Resolved once: the whole run — tools, prompt, archive — lands in
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

        guidance = _build_instructions(None if scope is None else self.scope_name)
        return PluginInstance(
            tools=tools,
            instructions=f"{guidance}\n\n{notes}",
            hooks=self._make_hooks(scope or self._user) if archived else None,
            aclose=repin,
        )

    # -- tools -------------------------------------------------------------------

    def _make_remember(self, scope: _Tier | None = None) -> Tool:
        plugin = self
        tiers = [self._user] if scope is None else [self._user, scope]

        async def save(fact: str, into: _Tier, replaces: str) -> str:
            # The date is the tool's to set: a model echoing a stamp it saw
            # in its notes would label today's fact as old.
            written = await plugin._write(
                _strip_date(_normalize_fact(fact)),
                into,
                replaces=replaces,
                among=tiers,
                budgeted=True,
            )
            reply = _WRITTEN_REPLY[written]
            if written == "full":
                reply += "\n\n" + await plugin._render_tier(into, scope is not None)
            return reply

        if scope is None:

            @tool(name="remember", description=_REMEMBER_DESCRIPTION)
            async def remember(
                fact: Annotated[str, _FACT_DOC],
                replaces: Annotated[str, _REPLACES_DOC] = "",
            ) -> str:
                return await save(fact, plugin._user, replaces)

            return remember

        mine: _Tier = scope

        async def remember_about(fact: str, about: str, replaces: str = "") -> str:
            return await save(fact, plugin._user if about == _USER else mine, replaces)

        # The choices for `about` are only known per Memory, so its annotation
        # is set here; @tool reads annotations objects and strings alike.
        about = Literal[(_USER, self.scope_name)]  # type: ignore[valid-type]
        remember_about.__annotations__.update(
            fact=Annotated[str, _FACT_DOC],
            about=Annotated[about, "Whom the fact is about."],
            replaces=Annotated[str, _REPLACES_DOC],
        )
        return tool(
            remember_about,
            name="remember",
            description=_REMEMBER_SCOPED_DESCRIPTION.format(name=self.scope_name),
        )

    def _make_forget(self, scope: _Tier | None = None) -> Tool:
        plugin = self
        tiers = [self._user] if scope is None else [self._user, scope]
        description = (
            _FORGET_DESCRIPTION
            if scope is None
            else _FORGET_SCOPED_DESCRIPTION.format(name=self.scope_name)
        )

        @tool(name="forget", description=description)
        async def forget(
            fact: Annotated[str, "The full text of the note to remove."],
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

    # -- end of run: index it ------------------------------------------------

    def _make_hooks(self, tier: _Tier) -> AgentHooks:
        hooks = AgentHooks()

        @hooks.on(RunCompleted)
        async def _on_completed(ev: RunCompleted, ctx: RunContext[Any]) -> None:
            # Best-effort: the run already completed.
            try:
                await self._archive(tier, ev.result.entries, ctx)
            except Exception:
                logger.warning("memory: archive ingest failed", exc_info=True)

        return hooks

    @staticmethod
    async def _archive(
        tier: _Tier, entries: list[TranscriptEntry], ctx: RunContext[Any]
    ) -> None:
        doc = _run_doc(entries, ctx)
        if doc is None:
            return
        if tier.key is None:
            # The user's stores are never swapped, and its lock is the one
            # every `remember` waits on: don't hold it across an embedding call.
            await cast(Index, tier.index).add([doc])
            return
        # clear_scope deletes the files and reopens the stores under this
        # lock: read the index inside it, never one captured before.
        async with tier.lock:
            if tier.index is not None:
                await tier.index.add([doc])


__all__ = [
    "FileNotesStore",
    "Memory",
    "NotesStore",
]
