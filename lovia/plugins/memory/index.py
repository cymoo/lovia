"""Cold-tier retrieval: the generic ``Index`` seam and its stdlib backends.

The cold tier of :class:`~lovia.plugins.memory.Memory` is plain text retrieval:
``add`` / ``remove`` / ``search`` over :class:`Doc`. The seam knows nothing
about lovia's transcript model — the plugin converts transcripts to docs — so a
third-party backend (a vector database, Elasticsearch, ...) is a small adapter
over three methods, written without reading any other lovia source.

Idempotency lives in the data, not the interface: ``Doc.id`` is an upsert key,
so re-adding the same id replaces the stored copy. Callers that derive ids
deterministically (as the Memory plugin does, from ``run_id`` + sequence) get
replace-on-retry semantics for free.

Two stdlib backends ship here, and compose with ``|`` into a Zep-style hybrid::

    KeywordIndex(path)                     # SQLite FTS5, CJK-aware bigrams, bm25
    KeywordIndex(path) | VectorIndex(...)  # HybridIndex: Reciprocal Rank Fusion
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from ...stores._sqlite import SQLiteStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The seam: Doc / Hit / Index
# ---------------------------------------------------------------------------


@dataclass
class Doc:
    """One unit of searchable memory: a self-contained piece of text.

    ``id`` is the upsert key — adding a doc whose id is already stored replaces
    the old copy. ``meta`` carries provenance (session/run ids, a ``kind`` tag,
    ...) that backends store verbatim and return on hits.
    """

    id: str
    text: str
    when: float = 0.0
    meta: dict[str, str] = field(default_factory=dict)


@dataclass
class Hit:
    """One search result: the stored doc plus a backend-specific score.

    Scores are only comparable within a single result list (bm25, cosine, and
    RRF live on different scales); higher is always better.
    """

    doc: Doc
    score: float


@runtime_checkable
class Index(Protocol):
    """The cold-tier seam: a searchable store of :class:`Doc`."""

    async def add(self, docs: list[Doc]) -> None:
        """Upsert docs by ``Doc.id``."""
        ...

    async def remove(self, ids: list[str]) -> None:
        """Delete the docs with these ids (missing ids are ignored)."""
        ...

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        """Return up to ``k`` hits relevant to ``query``, best first."""
        ...


class Fusable:
    """Mixin granting ``a | b`` composition into a :class:`HybridIndex`.

    Built-in indexes inherit it; a custom index can too, or compose explicitly
    with ``HybridIndex([...])``. Chains flatten: ``a | b | c`` is one three-arm
    hybrid, not a nested pair.
    """

    def __or__(self, other: Index) -> "HybridIndex":
        arms: list[Index] = []
        for side in (cast(Index, self), other):
            if isinstance(side, HybridIndex):
                arms.extend(side.indexes)
            else:
                arms.append(side)
        return HybridIndex(arms)


# ---------------------------------------------------------------------------
# CJK-aware term extraction (shared by KeywordIndex's FTS and LIKE paths).
#
# SQLite's default ``unicode61`` FTS tokenizer (and a plain ``LIKE``) can't
# segment scripts written without spaces between words: a whole CJK run becomes
# one token, so a query for "北京" never matches "...北京...". We split CJK runs
# into overlapping bigrams (other scripts' words stay whole) on both the indexed
# text and the query, so the two sides line up, two-character words match
# exactly, and bm25 still ranks.
# ---------------------------------------------------------------------------

# CJK Unified Ideographs (+ Ext. A, Compatibility), kana, and Hangul.
_CJK = "㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
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


# ---------------------------------------------------------------------------
# KeywordIndex: stdlib SQLite, FTS5 bm25 with a LIKE fallback
# ---------------------------------------------------------------------------

# ``text`` is stored for display only (UNINDEXED); ``search`` holds the
# bigram-segmented form that the default tokenizer actually indexes.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    id UNINDEXED,
    text UNINDEXED,
    search,
    when_ts UNINDEXED,
    meta UNINDEXED
);
"""

_PLAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_docs (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    when_ts REAL NOT NULL,
    meta TEXT NOT NULL
);
"""

# Tables from schema generations this module no longer reads. The cold tier is
# a recall cache, not a source of truth, so old rows are dropped, not migrated.
_LEGACY_TABLES = ("archive_fts", "archive_docs")


def _dump_meta(meta: dict[str, str]) -> str:
    return json.dumps(meta, ensure_ascii=False)


def _hit(row: sqlite3.Row, *, score: float) -> Hit:
    meta = cast(dict[str, str], json.loads(row["meta"])) if row["meta"] else {}
    return Hit(
        doc=Doc(id=row["id"], text=row["text"], when=row["when_ts"], meta=meta),
        score=score,
    )


class KeywordIndex(Fusable, SQLiteStore):
    """Default :class:`Index`: stdlib SQLite with FTS5 full-text search.

    Search ranks with bm25 over a CJK-aware bigram index (so scripts without
    whitespace word boundaries match too) when FTS5 is available, and falls
    back to a recency-ordered ``LIKE`` scan otherwise. Upsert-by-id is
    delete-then-insert on the FTS table (FTS5 has no primary key) and
    ``INSERT OR REPLACE`` on the fallback table.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._use_fts = _fts5_available()
        p = str(path)
        if p != ":memory:":
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(p, _FTS_SCHEMA if self._use_fts else _PLAIN_SCHEMA)
        self._table = "memory_fts" if self._use_fts else "memory_docs"
        self._reset_stale_tables()

    def _reset_stale_tables(self) -> None:
        """Drop tables this schema generation no longer reads.

        Covers pre-0.8 ``archive_*`` tables and any future ``memory_*`` shape
        change (detected by a missing current column). No data is migrated —
        the cold tier is a recall cache, and stale rows would otherwise sit in
        the file forever (or, for a same-name shape change, break ``add``).
        """
        conn = self._connect()
        try:
            stale = [
                name
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name IN (?, ?)",
                    _LEGACY_TABLES,
                )
            ]
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (self._table,)
            ).fetchone()
            if row and "meta" not in (row[0] or ""):
                stale.append(self._table)
            if not stale:
                return
            for name in stale:
                try:
                    conn.executescript(f"DROP TABLE IF EXISTS {name};")
                except sqlite3.OperationalError:
                    # e.g. an FTS5 virtual table in a build without FTS5 —
                    # unreadable either way; leave it inert.
                    logger.warning("memory: could not drop stale table %r", name)
            conn.executescript(self._schema)
            conn.commit()
        finally:
            self._release(conn)

    async def add(self, docs: list[Doc]) -> None:
        if not docs:
            return
        table = self._table
        rows: list[tuple[Any, ...]]
        if self._use_fts:
            insert = (
                f"INSERT INTO {table} (id, text, search, when_ts, meta) "
                "VALUES (?, ?, ?, ?, ?)"
            )
            rows = [
                (d.id, d.text, _index_text(d.text), d.when, _dump_meta(d.meta))
                for d in docs
            ]

            def _impl() -> None:
                conn = self._connect()
                try:
                    # Upsert: FTS5 has no primary key, so replace explicitly.
                    conn.executemany(
                        f"DELETE FROM {table} WHERE id = ?", [(d.id,) for d in docs]
                    )
                    conn.executemany(insert, rows)
                    conn.commit()
                finally:
                    self._release(conn)

        else:
            insert = (
                f"INSERT OR REPLACE INTO {table} (id, text, when_ts, meta) "
                "VALUES (?, ?, ?, ?)"
            )
            rows = [(d.id, d.text, d.when, _dump_meta(d.meta)) for d in docs]

            def _impl() -> None:
                conn = self._connect()
                try:
                    conn.executemany(insert, rows)
                    conn.commit()
                finally:
                    self._release(conn)

        await self._run(_impl)

    async def remove(self, ids: list[str]) -> None:
        if not ids:
            return
        table = self._table

        def _impl() -> None:
            conn = self._connect()
            try:
                conn.executemany(
                    f"DELETE FROM {table} WHERE id = ?", [(i,) for i in ids]
                )
                conn.commit()
            finally:
                self._release(conn)

        await self._run(_impl)

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        terms = _terms(query)
        if not terms:
            return []

        if self._use_fts:
            # Quote each term so a CJK bigram or ASCII word is one FTS phrase;
            # OR them and let bm25 rank by how many distinct terms each row hits.
            match = " OR ".join(f'"{t}"' for t in terms)

            def _fts() -> list[Hit]:
                conn = self._connect()
                try:
                    rows = conn.execute(
                        "SELECT id, text, when_ts, meta, bm25(memory_fts) AS score "
                        "FROM memory_fts WHERE memory_fts MATCH ? "
                        "ORDER BY score LIMIT ?",
                        (match, k),
                    ).fetchall()
                finally:
                    self._release(conn)
                # bm25 returns lower = better; report -bm25 so higher = better.
                return [_hit(r, score=-float(r["score"])) for r in rows]

            return await self._run(_fts)

        clause = " OR ".join(["text LIKE ?"] * len(terms))
        params: list[Any] = [f"%{t}%" for t in terms] + [k]

        def _like() -> list[Hit]:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, text, when_ts, meta FROM memory_docs "
                    f"WHERE {clause} ORDER BY when_ts DESC LIMIT ?",
                    params,
                ).fetchall()
            finally:
                self._release(conn)
            return [_hit(r, score=0.0) for r in rows]

        return await self._run(_like)


# ---------------------------------------------------------------------------
# HybridIndex: Reciprocal Rank Fusion over any set of indexes
# ---------------------------------------------------------------------------


class HybridIndex(Fusable):
    """Fuse several indexes into one with Reciprocal Rank Fusion.

    Each arm contributes ``1 / (rrf_k + rank)`` per doc (dedup by ``Doc.id``);
    rank-based fusion needs no score normalization across arms, and ``rrf_k=60``
    is the standard zero-tuning baseline. Writes broadcast to every arm and
    propagate failures (retrying an ``add`` is safe — ids upsert). Reads fail
    open: a broken arm is logged and skipped so one unreachable backend
    degrades recall instead of disabling it; only every arm failing raises.
    """

    def __init__(self, indexes: Sequence[Index], *, rrf_k: int = 60) -> None:
        if not indexes:
            raise ValueError("HybridIndex needs at least one index")
        self.indexes: list[Index] = list(indexes)
        self._rrf_k = rrf_k

    async def add(self, docs: list[Doc]) -> None:
        await self._broadcast([arm.add(docs) for arm in self.indexes])

    async def remove(self, ids: list[str]) -> None:
        await self._broadcast([arm.remove(ids) for arm in self.indexes])

    @staticmethod
    async def _broadcast(calls: list[Any]) -> None:
        # Let every arm finish before propagating, so one failing arm can't
        # leave siblings mid-write with their tasks abandoned.
        results = await asyncio.gather(*calls, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        # Over-fetch per arm: a doc ranked deep in one arm can still fuse into
        # the overall top-k.
        results = await asyncio.gather(
            *(arm.search(query, 2 * k) for arm in self.indexes),
            return_exceptions=True,
        )
        ranked: list[list[Hit]] = []
        for arm, r in zip(self.indexes, results):
            if isinstance(r, BaseException):
                logger.warning(
                    "memory: hybrid arm %r failed to search; skipping it",
                    type(arm).__name__,
                    exc_info=r,
                )
            else:
                ranked.append(r)
        if not ranked:
            for r in results:
                if isinstance(r, BaseException):
                    raise r
            return []

        fused: dict[str, Hit] = {}
        for arm_hits in ranked:
            for rank, hit in enumerate(arm_hits):
                gain = 1.0 / (self._rrf_k + rank + 1)
                seen = fused.get(hit.doc.id)
                if seen is None:
                    # Keep the first arm's doc; report the fused score.
                    fused[hit.doc.id] = Hit(doc=hit.doc, score=gain)
                else:
                    seen.score += gain
        out = sorted(fused.values(), key=lambda h: h.score, reverse=True)
        return out[:k]


__all__ = [
    "Doc",
    "Fusable",
    "Hit",
    "HybridIndex",
    "Index",
    "KeywordIndex",
]
