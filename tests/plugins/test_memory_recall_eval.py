"""Opt-in recall-quality eval — issue #39 step 4.

Compares retrieval arms on the three query categories that motivated the
issue (exact identifiers, paraphrase, cross-lingual), reporting hit@3 per
category:

* ``keyword``        — KeywordIndex, raw query
* ``keyword+expand`` — KeywordIndex, query expanded by the live chat model
* ``vector``         — VectorIndex (only when an embeddings endpoint is set)
* ``hybrid``         — keyword | vector (ditto)

Run with the chat endpoint from ``.env``::

    LOVIA_LIVE_TESTS=1 pytest tests/plugins/test_memory_recall_eval.py -m live_provider -s

Set ``LOVIA_EMBEDDING_MODEL`` (plus ``LOVIA_EMBEDDING_BASE_URL`` /
``LOVIA_EMBEDDING_API_KEY`` if embeddings live on a different host than
chat) to include the vector and hybrid arms.
"""

from __future__ import annotations

import os

import pytest

from lovia.plugins.memory.index import Doc, Index, KeywordIndex
from lovia.plugins.memory.plugin import _expand
from lovia.plugins.memory.vector import OpenAIEmbedder, VectorIndex

from .test_memory import _live_model

CORPUS = [
    Doc(
        id="refund",
        text="Our refund policy: customers can get their money back within 30 days of purchase.",
    ),
    Doc(id="order", text="Order ORD-88317 shipped on Monday via DHL express."),
    Doc(
        id="dog", text="The user has a golden retriever named Rex who loves the beach."
    ),
    Doc(
        id="kyoto",
        text="Planning a November trip to Kyoto to see the autumn maple foliage.",
    ),
    Doc(
        id="python",
        text="The user strongly prefers Python over JavaScript for all code examples.",
    ),
    Doc(
        id="meeting",
        text="The quarterly review meeting is scheduled for Friday at 3pm with Alice.",
    ),
    Doc(
        id="server", text="The staging server IP is 10.0.4.17 and it runs Ubuntu 22.04."
    ),
    Doc(
        id="coffee", text="The user drinks oat-milk lattes and dislikes sugary drinks."
    ),
]

# (query, expected doc id, category). The pet queries are deliberately
# unreachable by keywords even after expansion — the doc says "golden
# retriever named Rex", never "pet" or "dog" — marking the residual gap that
# only the semantic arm closes.
QUERIES = [
    ("ORD-88317", "order", "exact-id"),
    ("10.0.4.17", "server", "exact-id"),
    ("Kyoto maple foliage", "kyoto", "keyword"),
    ("refund policy", "refund", "keyword"),
    ("how can I get my money back", "refund", "paraphrase"),
    ("what kind of pet does the user have", "dog", "paraphrase"),
    ("the user's favorite programming language", "python", "paraphrase"),
    ("退款政策是什么", "refund", "cross-lingual"),
    ("去京都赏红叶的计划", "kyoto", "cross-lingual"),
    ("用户养了什么宠物", "dog", "cross-lingual"),
]

CATEGORIES = ("exact-id", "keyword", "paraphrase", "cross-lingual")
K = 3


async def _score(index: Index, expander=None) -> tuple[dict[str, str], int]:
    """Per-category hit@K as ``hits/total`` display strings, plus the raw total."""
    per_cat = {c: [0, 0] for c in CATEGORIES}
    total = 0
    for query, expected, category in QUERIES:
        search_query = query
        if expander is not None:
            terms = await expander(query)
            if terms:
                search_query = f"{query} {' '.join(terms)}"
        hits = await index.search(search_query, K)
        hit = int(any(h.doc.id == expected for h in hits))
        per_cat[category][0] += hit
        per_cat[category][1] += 1
        total += hit
    row = {c: f"{h}/{n}" for c, (h, n) in per_cat.items()}
    row["TOTAL"] = f"{total}/{len(QUERIES)}"
    return row, total


def _print_table(rows: dict[str, dict[str, str]]) -> None:
    cols = (*CATEGORIES, "TOTAL")
    width = max(len(name) for name in rows)
    print(f"\n{'arm'.ljust(width)}  " + "  ".join(c.rjust(13) for c in cols))
    for name, row in rows.items():
        print(f"{name.ljust(width)}  " + "  ".join(str(row[c]).rjust(13) for c in cols))


def _embedder_from_env() -> OpenAIEmbedder | None:
    model = os.getenv("LOVIA_EMBEDDING_MODEL")
    if not model:
        return None
    return OpenAIEmbedder(model)


@pytest.mark.live_provider
async def test_recall_eval(tmp_path) -> None:
    chat_model = f"openai:{_live_model()}"

    keyword = KeywordIndex(tmp_path / "kw.db")
    await keyword.add(CORPUS)

    async def expander(query: str) -> list[str]:
        return await _expand(query, chat_model)

    rows: dict[str, dict[str, str]] = {}
    totals: dict[str, int] = {}
    rows["keyword"], totals["keyword"] = await _score(keyword)
    rows["keyword+expand"], totals["keyword+expand"] = await _score(keyword, expander)

    embedder = _embedder_from_env()
    if embedder is not None:
        vector = VectorIndex(tmp_path / "vec.db", embedder)
        await vector.add(CORPUS)
        rows["vector"], totals["vector"] = await _score(vector)
        rows["hybrid"], totals["hybrid"] = await _score(keyword | vector)

    _print_table(rows)

    def hits(arm: str, category: str) -> int:
        return int(rows[arm][category].split("/")[0])

    # Lexical search nails exact identifiers and literal keywords by design.
    assert rows["keyword"]["exact-id"] == "2/2"
    assert rows["keyword"]["keyword"] == "2/2"
    # Expansion rides along with the raw query, so it can only add recall on
    # this corpus — and it should recover cross-lingual misses.
    assert totals["keyword+expand"] >= totals["keyword"]
    assert hits("keyword+expand", "cross-lingual") > hits("keyword", "cross-lingual")
    if embedder is not None:
        # The hybrid keeps keyword's exact-id strength while adding semantics.
        assert rows["hybrid"]["exact-id"] == "2/2"
        assert totals["hybrid"] >= totals["keyword"]
