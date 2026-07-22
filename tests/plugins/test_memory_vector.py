"""Tests for the semantic cold tier: OpenAIEmbedder (over httpx.MockTransport)
and VectorIndex (with a deterministic fake embedder), plus keyword|vector
hybrid recall — the headline scenario from issue #39.

All network-free.
"""

from __future__ import annotations

import json

import httpx
import pytest

from lovia.exceptions import ProviderError, UserError
from lovia.plugins.memory.index import Doc, KeywordIndex, _fts5_available
from lovia.plugins.memory.vector import OpenAIEmbedder, VectorIndex

requires_fts = pytest.mark.skipif(
    not _fts5_available(), reason="SQLite built without FTS5"
)


class FakeEmbedder:
    """Deterministic 4-dim embedder built on synonym/translation buckets.

    Texts sharing a bucket word ("dog" / "chien" / "狗") land near each other,
    so tests exercise real semantic behavior — cross-lingual and synonym
    matching — without a model.
    """

    BUCKETS = (
        ("dog", "puppy", "chien", "狗"),
        ("car", "automobile", "汽车"),
        ("trip", "travel", "旅行", "kyoto"),
    )

    def __init__(self, id: str = "fake:v1") -> None:
        self.id = id
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        out = []
        for text in texts:
            low = text.lower()
            v = [float(sum(low.count(w) for w in bucket)) for bucket in self.BUCKETS]
            v.append(0.1)  # base component so no vector is all-zero
            out.append(v)
        return out


def _embeddings_transport(recorded: list[httpx.Request], *, dim: int = 3):
    """A MockTransport answering /embeddings with per-input constant vectors."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        inputs = json.loads(request.content)["input"]
        data = [
            # Reversed order on purpose: clients must sort by "index".
            {"index": i, "embedding": [float(i + 1)] * dim}
            for i in reversed(range(len(inputs)))
        ]
        return httpx.Response(200, json={"data": data})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# OpenAIEmbedder
# ---------------------------------------------------------------------------


async def test_embedder_payload_parsing_and_index_order(monkeypatch) -> None:
    monkeypatch.delenv("LOVIA_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    requests: list[httpx.Request] = []
    emb = OpenAIEmbedder(
        "text-embedding-3-small",
        api_key="sk-test",
        client=httpx.AsyncClient(transport=_embeddings_transport(requests)),
    )
    vectors = await emb.embed(["alpha", "bravo"])
    # Response arrived reversed; "index" restores input order.
    assert vectors == [[1.0, 1.0, 1.0], [2.0, 2.0, 2.0]]
    (req,) = requests
    assert req.url == "https://api.openai.com/v1/embeddings"
    assert req.headers["Authorization"] == "Bearer sk-test"
    payload = json.loads(req.content)
    assert payload == {"model": "text-embedding-3-small", "input": ["alpha", "bravo"]}
    await emb.aclose()


async def test_embedder_dimensions_param_and_id() -> None:
    requests: list[httpx.Request] = []
    emb = OpenAIEmbedder(
        "text-embedding-3-small",
        api_key="k",
        dimensions=256,
        client=httpx.AsyncClient(transport=_embeddings_transport(requests)),
    )
    assert emb.id == "openai:text-embedding-3-small:256"
    assert OpenAIEmbedder("m", api_key="k").id == "openai:m"
    await emb.embed(["x"])
    assert json.loads(requests[0].content)["dimensions"] == 256


async def test_embedder_batches_requests() -> None:
    requests: list[httpx.Request] = []
    emb = OpenAIEmbedder(
        "m",
        api_key="k",
        batch_size=2,
        client=httpx.AsyncClient(transport=_embeddings_transport(requests)),
    )
    vectors = await emb.embed(["a", "b", "c", "d", "e"])
    assert len(vectors) == 5
    assert [len(json.loads(r.content)["input"]) for r in requests] == [2, 2, 1]


async def test_embedder_empty_input_sends_nothing() -> None:
    requests: list[httpx.Request] = []
    emb = OpenAIEmbedder(
        "m",
        api_key="k",
        client=httpx.AsyncClient(transport=_embeddings_transport(requests)),
    )
    assert await emb.embed([]) == []
    assert requests == []


async def test_embedder_http_error_is_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    emb = OpenAIEmbedder(
        "m",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError) as exc_info:
        await emb.embed(["x"])
    assert exc_info.value.status_code == 429
    assert exc_info.value.retryable is True

    def handler4(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    emb4 = OpenAIEmbedder(
        "m",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler4)),
    )
    with pytest.raises(ProviderError) as exc_info:
        await emb4.embed(["x"])
    assert exc_info.value.retryable is False


async def test_embedder_malformed_response_is_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": []})

    emb = OpenAIEmbedder(
        "m",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError, match="not in OpenAI format"):
        await emb.embed(["x"])


async def test_embedder_count_mismatch_is_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    emb = OpenAIEmbedder(
        "m",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(ProviderError, match="1 vectors for 2 inputs"):
        await emb.embed(["x", "y"])


async def test_embedder_official_endpoint_requires_key(monkeypatch) -> None:
    for var in (
        "OPENAI_API_KEY",
        "LOVIA_EMBEDDING_API_KEY",
        "OPENAI_BASE_URL",
        "LOVIA_EMBEDDING_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(UserError, match="requires an API key"):
        await OpenAIEmbedder("m").embed(["x"])


def test_embedder_env_fallbacks(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://chat.example/v1")
    monkeypatch.setenv("LOVIA_EMBEDDING_BASE_URL", "https://embed.example/v1/")
    monkeypatch.setenv("OPENAI_API_KEY", "chat-key")
    monkeypatch.setenv("LOVIA_EMBEDDING_API_KEY", "embed-key")
    emb = OpenAIEmbedder("m")
    # The EMBEDDING-specific variables win (chat and embeddings often live on
    # different hosts); trailing slash is trimmed.
    assert emb.base_url == "https://embed.example/v1"
    assert emb._headers()["Authorization"] == "Bearer embed-key"
    monkeypatch.delenv("LOVIA_EMBEDDING_BASE_URL")
    assert OpenAIEmbedder("m").base_url == "https://chat.example/v1"


# ---------------------------------------------------------------------------
# VectorIndex
# ---------------------------------------------------------------------------


async def test_vector_semantic_and_crosslingual_recall() -> None:
    idx = VectorIndex(":memory:", FakeEmbedder())
    await idx.add(
        [
            Doc(id="pet", text="I have a dog named Rex"),
            Doc(id="drive", text="my car broke down yesterday"),
        ]
    )
    # Vocabulary mismatch: no shared token, same meaning.
    hits = await idx.search("chien")
    assert hits and hits[0].doc.id == "pet"
    # Cross-lingual.
    hits = await idx.search("狗")
    assert hits[0].doc.id == "pet"
    hits = await idx.search("automobile")
    assert hits[0].doc.id == "drive"


async def test_vector_ranking_by_similarity() -> None:
    idx = VectorIndex(":memory:", FakeEmbedder())
    await idx.add(
        [
            Doc(id="strong", text="dog dog puppy"),
            Doc(id="weak", text="a dog appeared in kyoto on my travel trip"),
        ]
    )
    hits = await idx.search("chien", k=2)
    assert [h.doc.id for h in hits] == ["strong", "weak"]
    assert hits[0].score > hits[1].score


async def test_vector_upsert_remove_and_roundtrip() -> None:
    idx = VectorIndex(":memory:", FakeEmbedder())
    meta = {"session_id": "s1", "kind": "summary"}
    await idx.add([Doc(id="a", text="my dog Rex", when=42.0, meta=meta)])
    (hit,) = await idx.search("puppy")
    assert (hit.doc.when, hit.doc.meta) == (42.0, meta)
    # Upsert: same id, new text — the old vector is replaced.
    await idx.add([Doc(id="a", text="my car is red")])
    assert (await idx.search("automobile"))[0].doc.id == "a"
    hits = await idx.search("puppy")
    assert not hits or hits[0].score == pytest.approx(
        0.0, abs=0.2
    )  # only base-component residue
    await idx.remove(["a", "missing"])
    assert await idx.search("automobile") == []
    await idx.add([])  # no-ops
    await idx.remove([])


async def test_vector_empty_query_and_empty_index_skip_embedding() -> None:
    emb = FakeEmbedder()
    idx = VectorIndex(":memory:", emb)
    assert await idx.search("   ") == []
    assert await idx.search("dog") == []  # empty index: no embedding call
    assert emb.calls == []
    await idx.add([Doc(id="a", text="dog")])
    assert await idx.search("dog", k=0) == []


async def test_vector_resets_when_embedder_changes(tmp_path) -> None:
    path = tmp_path / "vectors.db"
    idx = VectorIndex(path, FakeEmbedder("fake:v1"))
    await idx.add([Doc(id="a", text="my dog Rex")])
    # Same space on reopen: docs survive.
    again = VectorIndex(path, FakeEmbedder("fake:v1"))
    assert await again.search("dog")
    # Different space: stored vectors are incomparable, index resets.
    respaced = VectorIndex(path, FakeEmbedder("fake:v2"))
    assert await respaced.search("dog") == []
    # And the new space works from there.
    await respaced.add([Doc(id="b", text="my dog Rex")])
    assert await respaced.search("dog")


async def test_vector_count_mismatch_raises() -> None:
    class Broken(FakeEmbedder):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0]]

    idx = VectorIndex(":memory:", Broken())
    with pytest.raises(ProviderError, match="1 vectors"):
        await idx.add([Doc(id="a", text="x"), Doc(id="b", text="y")])


async def test_vector_all_zero_vectors_do_not_crash() -> None:
    class Zero(FakeEmbedder):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return [[0.0, 0.0, 0.0] for _ in texts]

    idx = VectorIndex(":memory:", Zero())
    await idx.add([Doc(id="a", text="whatever")])
    hits = await idx.search("query")
    assert hits and hits[0].score == 0.0


# ---------------------------------------------------------------------------
# Hybrid: keyword | vector — the issue #39 headline
# ---------------------------------------------------------------------------


@requires_fts
async def test_hybrid_keyword_vector_covers_both_failure_modes() -> None:
    hybrid = KeywordIndex(":memory:") | VectorIndex(":memory:", FakeEmbedder())
    await hybrid.add(
        [
            Doc(id="pet", text="I have a dog named Rex"),
            Doc(id="order", text="the order number is ORD-12345"),
        ]
    )
    # Semantic arm catches what keywords cannot: "chien" shares no token with
    # the dog doc.
    hits = await hybrid.search("chien")
    assert hits and hits[0].doc.id == "pet"
    # Keyword arm catches what embeddings are weak at: an exact identifier
    # (the fake embedder maps it near zero).
    hits = await hybrid.search("ORD-12345")
    assert hits and hits[0].doc.id == "order"
    # A doc matching both arms fuses above one matching a single arm.
    hits = await hybrid.search("dog")
    assert hits[0].doc.id == "pet"
