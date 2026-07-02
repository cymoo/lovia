"""Tests for the memory cold-tier retrieval layer: Doc/Hit/Index, KeywordIndex,
HybridIndex (RRF fusion), and the ``|`` composition sugar.

All network-free; KeywordIndex runs on in-memory SQLite.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from lovia.plugins.memory import index as index_mod
from lovia.plugins.memory.index import (
    Doc,
    Fusable,
    Hit,
    HybridIndex,
    KeywordIndex,
    _fts5_available,
    _terms,
)

requires_fts = pytest.mark.skipif(
    not _fts5_available(), reason="SQLite built without FTS5"
)


def _docs(*texts: str, prefix: str = "d", when: float = 1.0) -> list[Doc]:
    return [Doc(id=f"{prefix}{i}", text=t, when=when) for i, t in enumerate(texts)]


class FakeIndex(Fusable):
    """Scriptable in-memory Index that records writes and replays hits."""

    def __init__(self, hits: list[Hit] | None = None, *, fail: bool = False) -> None:
        self.hits = hits or []
        self.fail = fail
        self.added: list[Doc] = []
        self.removed: list[str] = []

    async def add(self, docs: list[Doc]) -> None:
        if self.fail:
            raise RuntimeError("add boom")
        self.added.extend(docs)

    async def remove(self, ids: list[str]) -> None:
        if self.fail:
            raise RuntimeError("remove boom")
        self.removed.extend(ids)

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        if self.fail:
            raise RuntimeError("search boom")
        return self.hits[:k]


def _hit(doc_id: str, text: str = "", score: float = 1.0) -> Hit:
    return Hit(doc=Doc(id=doc_id, text=text or doc_id), score=score)


# ---------------------------------------------------------------------------
# Term segmentation
# ---------------------------------------------------------------------------


def test_terms_segmentation() -> None:
    assert _terms("hiking Mountains") == ["hiking", "mountains"]
    assert _terms("北京") == ["北京"]
    assert _terms("北京出差") == ["北京", "京出", "出差"]
    assert _terms("我爱python") == ["我爱", "python"]
    assert _terms("café Müller") == ["café", "müller"]  # accented Latin: whole
    assert _terms("Москва") == ["москва"]  # Cyrillic: whole, not bigrammed
    assert _terms("中") == ["中"]
    assert _terms("") == []
    assert _terms("!!! ???") == []


# ---------------------------------------------------------------------------
# KeywordIndex — FTS5 path
# ---------------------------------------------------------------------------


@requires_fts
async def test_keyword_ranking_and_filtering() -> None:
    idx = KeywordIndex(":memory:")
    assert idx._use_fts
    await idx.add(_docs("I love hiking in the mountains", prefix="hike"))
    await idx.add(_docs("best pasta recipe with guanciale", prefix="food"))
    hits = await idx.search("hiking mountains", k=5)
    assert hits
    assert all("hik" in h.doc.text or "mountain" in h.doc.text for h in hits)
    # bm25-ranked: we report -bm25, so higher score == better; results best-first.
    assert hits == sorted(hits, key=lambda h: h.score, reverse=True)


@requires_fts
async def test_keyword_cjk_search() -> None:
    idx = KeywordIndex(":memory:")
    await idx.add(
        [
            Doc(id="a", text="我今天去了北京出差，顺便看了朋友"),
            Doc(id="b", text="北京很好玩，我爱 python"),
        ]
    )
    # Two-char CJK words match: the default unicode61 tokenizer keeps a whole
    # CJK run as one token and misses these; the bigram index segments them.
    assert await idx.search("北京")
    assert await idx.search("出差")
    # A natural-language CJK query matches via its bigrams, not just exact words.
    assert await idx.search("我想知道北京出差的情况")
    # Mixed CJK + ASCII: the ASCII word is found too.
    assert await idx.search("python")
    assert await idx.search("广州") == []
    # bm25 ranks the doc with more matching bigrams first.
    hits = await idx.search("北京出差")
    assert hits[0].doc.id == "a"


@requires_fts
async def test_keyword_upsert_by_id_replaces() -> None:
    idx = KeywordIndex(":memory:")
    await idx.add([Doc(id="x", text="alpha alpha")])
    await idx.add([Doc(id="x", text="bravo bravo")])  # same id → replace
    assert await idx.search("alpha") == []
    hits = await idx.search("bravo")
    assert [h.doc.id for h in hits] == ["x"]


@requires_fts
async def test_keyword_remove() -> None:
    idx = KeywordIndex(":memory:")
    await idx.add([Doc(id="a", text="alpha"), Doc(id="b", text="bravo")])
    await idx.remove(["a", "missing"])  # missing ids are ignored
    assert await idx.search("alpha") == []
    assert await idx.search("bravo")
    await idx.remove([])  # no-op, no error


@requires_fts
async def test_keyword_meta_and_when_roundtrip() -> None:
    idx = KeywordIndex(":memory:")
    meta = {"session_id": "s1", "kind": "message", "语言": "中文"}
    await idx.add([Doc(id="a", text="zebra fact", when=1234.5, meta=meta)])
    (hit,) = await idx.search("zebra")
    assert hit.doc.id == "a"
    assert hit.doc.when == 1234.5
    assert hit.doc.meta == meta


async def test_keyword_empty_add_and_empty_query() -> None:
    idx = KeywordIndex(":memory:")
    await idx.add([])  # no-op
    await idx.add([Doc(id="a", text="hello")])
    assert await idx.search("") == []
    assert await idx.search("!!! ??? ...") == []


async def test_keyword_on_disk_roundtrip(tmp_path) -> None:
    # Constructing under a missing dir must not fail (parent is created), and
    # a second instance over the same file sees the first one's docs.
    path = tmp_path / "nested" / "index.db"
    idx = KeywordIndex(path)
    await idx.add([Doc(id="a", text="persistent zebra fact")])
    assert path.exists()
    again = KeywordIndex(path)
    hits = await again.search("zebra")
    assert [h.doc.id for h in hits] == ["a"]


@requires_fts
async def test_keyword_drops_legacy_archive_tables(tmp_path) -> None:
    # A pre-0.8 archive.db holds archive_fts/archive_docs tables this module no
    # longer reads; they are dropped at init so stale rows don't sit in the
    # file forever. (The cold tier is a recall cache — no data migration.)
    path = tmp_path / "archive.db"
    con = sqlite3.connect(str(path))
    con.executescript(
        "CREATE VIRTUAL TABLE archive_fts USING fts5("
        "session_id UNINDEXED, run_id UNINDEXED, text UNINDEXED, search, "
        "when_ts UNINDEXED);"
        "CREATE TABLE archive_docs (id INTEGER PRIMARY KEY, text TEXT);"
    )
    con.commit()
    con.close()

    idx = KeywordIndex(path)
    await idx.add([Doc(id="a", text="fresh hiking trip")])
    assert await idx.search("hiking")

    con = sqlite3.connect(str(path))
    names = {
        n for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    con.close()
    assert "archive_fts" not in names
    assert "archive_docs" not in names


@requires_fts
async def test_keyword_rebuilds_stale_schema(tmp_path) -> None:
    # A memory_fts table from an older shape (no meta column) is rebuilt empty
    # rather than left to crash add().
    path = tmp_path / "index.db"
    con = sqlite3.connect(str(path))
    con.executescript(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "id UNINDEXED, text UNINDEXED, search, when_ts UNINDEXED);"
    )
    con.execute(
        "INSERT INTO memory_fts VALUES ('old', 'stale relic', 'stale relic', 0)"
    )
    con.commit()
    con.close()

    idx = KeywordIndex(path)
    await idx.add([Doc(id="a", text="fresh hiking trip")])
    assert await idx.search("hiking")
    assert await idx.search("relic") == []  # old rows discarded, not migrated


# ---------------------------------------------------------------------------
# KeywordIndex — LIKE fallback path
# ---------------------------------------------------------------------------


async def test_keyword_like_fallback(monkeypatch) -> None:
    monkeypatch.setattr(index_mod, "_fts5_available", lambda: False)
    idx = KeywordIndex(":memory:")
    assert not idx._use_fts
    await idx.add([Doc(id="a", text="I love hiking in the mountains")])
    hits = await idx.search("hiking", k=5)
    assert hits and hits[0].doc.id == "a"
    assert await idx.search("   ") == []


async def test_keyword_like_fallback_cjk_and_upsert(monkeypatch) -> None:
    monkeypatch.setattr(index_mod, "_fts5_available", lambda: False)
    idx = KeywordIndex(":memory:")
    await idx.add([Doc(id="a", text="我今天去了北京出差")])
    # The LIKE fallback segments CJK queries into bigrams too.
    assert await idx.search("北京")
    assert await idx.search("我想知道北京出差的情况")
    assert await idx.search("广州") == []
    # Upsert via primary key.
    await idx.add([Doc(id="a", text="改去上海了")])
    assert await idx.search("北京") == []
    assert await idx.search("上海")
    # Remove works on the fallback table as well.
    await idx.remove(["a"])
    assert await idx.search("上海") == []


# ---------------------------------------------------------------------------
# ``|`` composition
# ---------------------------------------------------------------------------


def test_or_composes_and_flattens() -> None:
    a, b, c = FakeIndex(), FakeIndex(), FakeIndex()
    ab = a | b
    assert isinstance(ab, HybridIndex)
    assert ab.indexes == [a, b]
    # Chains flatten instead of nesting, in both association orders.
    assert (ab | c).indexes == [a, b, c]
    assert (a | (b | c)).indexes == [a, b, c]


def test_hybrid_requires_at_least_one_arm() -> None:
    with pytest.raises(ValueError):
        HybridIndex([])


# ---------------------------------------------------------------------------
# HybridIndex — RRF fusion
# ---------------------------------------------------------------------------


async def test_hybrid_rrf_ranks_multi_arm_docs_first() -> None:
    # arm1 ranks [A, B]; arm2 ranks [B, C]. B appears in both arms, so its
    # fused score 1/62 + 1/61 beats A's 1/61 and C's 1/62.
    arm1 = FakeIndex([_hit("A"), _hit("B")])
    arm2 = FakeIndex([_hit("B"), _hit("C")])
    hits = await HybridIndex([arm1, arm2]).search("q", k=3)
    assert [h.doc.id for h in hits] == ["B", "A", "C"]
    assert hits[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert hits[1].score == pytest.approx(1 / 61)
    assert hits[2].score == pytest.approx(1 / 62)


async def test_hybrid_dedups_by_id_keeping_first_arm_doc() -> None:
    arm1 = FakeIndex([Hit(doc=Doc(id="X", text="from arm1"), score=9.0)])
    arm2 = FakeIndex([Hit(doc=Doc(id="X", text="from arm2"), score=0.1)])
    hits = await HybridIndex([arm1, arm2]).search("q")
    assert len(hits) == 1
    assert hits[0].doc.text == "from arm1"


async def test_hybrid_truncates_to_k() -> None:
    arm = FakeIndex([_hit(f"d{i}") for i in range(10)])
    hits = await HybridIndex([arm]).search("q", k=3)
    assert len(hits) == 3


async def test_hybrid_search_fails_open_per_arm(caplog) -> None:
    healthy = FakeIndex([_hit("A")])
    broken = FakeIndex(fail=True)
    with caplog.at_level(logging.WARNING, logger="lovia.plugins.memory.index"):
        hits = await HybridIndex([broken, healthy]).search("q")
    assert [h.doc.id for h in hits] == ["A"]
    assert any("failed to search" in r.message for r in caplog.records)


async def test_hybrid_search_raises_when_all_arms_fail() -> None:
    with pytest.raises(RuntimeError, match="search boom"):
        await HybridIndex([FakeIndex(fail=True), FakeIndex(fail=True)]).search("q")


async def test_hybrid_add_remove_broadcast() -> None:
    a, b = FakeIndex(), FakeIndex()
    hybrid = HybridIndex([a, b])
    docs = _docs("one", "two")
    await hybrid.add(docs)
    assert a.added == docs and b.added == docs
    await hybrid.remove(["d0"])
    assert a.removed == ["d0"] and b.removed == ["d0"]


async def test_hybrid_add_propagates_failure_after_all_arms_ran() -> None:
    healthy, broken = FakeIndex(), FakeIndex(fail=True)
    hybrid = HybridIndex([broken, healthy])
    with pytest.raises(RuntimeError, match="add boom"):
        await hybrid.add(_docs("one"))
    # The healthy sibling still received the write (retry is a safe upsert).
    assert [d.text for d in healthy.added] == ["one"]


@requires_fts
async def test_hybrid_over_keyword_arms_end_to_end() -> None:
    # Two real keyword indexes with different corpora: fusion surfaces docs
    # from both, and a doc present in both arms ranks first.
    arm1, arm2 = KeywordIndex(":memory:"), KeywordIndex(":memory:")
    await arm1.add(
        [Doc(id="both", text="tokyo travel plan"), Doc(id="a1", text="tokyo ramen")]
    )
    await arm2.add(
        [Doc(id="both", text="tokyo travel plan"), Doc(id="a2", text="tokyo museums")]
    )
    hits = await (arm1 | arm2).search("tokyo", k=5)
    ids = [h.doc.id for h in hits]
    assert ids[0] == "both"
    assert {"a1", "a2"} <= set(ids)
