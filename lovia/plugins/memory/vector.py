"""Semantic cold-tier retrieval: the ``Embedder`` seam and a stdlib vector index.

This is the opt-in semantic arm from issue #39. The moving parts:

* :class:`Embedder` — one method, ``embed(texts) -> vectors``, plus an ``id``
  naming the vector space. Swap it to change models without touching storage.
* :class:`OpenAIEmbedder` — calls any OpenAI-compatible ``/embeddings``
  endpoint through lovia's existing httpx dependency. No new packages.
* :class:`VectorIndex` — embeddings in SQLite, brute-force cosine search.
  At lovia's scale (thousands to tens of thousands of docs on one machine)
  a full scan is milliseconds; an ANN library buys nothing until far beyond
  that. Users who outgrow it implement :class:`~.index.Index` over a real
  vector store instead.

Compose with the keyword arm for Zep-style hybrid recall::

    KeywordIndex(root / "archive.db") | VectorIndex(root / "vectors.db", embedder)
"""

from __future__ import annotations

import logging
import math
import operator
import os
from array import array
from collections.abc import Iterable, Sequence
from heapq import nlargest
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

import httpx

from ...exceptions import ProviderError, UserError
from ...http_config import resolve_timeout, resolve_trust_env, resolve_verify
from ...providers._http import is_retryable_status
from ...stores._sqlite import SQLiteStore
from .index import Doc, Fusable, Hit, _dump_meta, _hit

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The seam: Embedder
# ---------------------------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """Maps texts to fixed-dimension vectors for semantic search.

    ``id`` names the vector space (model + output dimensions): vectors from
    different spaces are not comparable, so :class:`VectorIndex` persists the
    id and resets its stored vectors when it changes.
    """

    id: str

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one vector per input text, in input order."""
        ...


# ---------------------------------------------------------------------------
# OpenAIEmbedder: any OpenAI-compatible /embeddings endpoint
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIEmbedder:
    """OpenAI-compatible ``/embeddings`` API adapter.

    Works against the official API and any compatible endpoint (multilingual
    models like BGE-M3 on SiliconFlow, DashScope, Jina, local servers, ...).

    Args:
        model: The embedding model identifier sent to the API.
        api_key: API key. Defaults to ``$OPENAI_EMBEDDING_API_KEY``, then
            ``$OPENAI_API_KEY``.
        base_url: Override to target a compatible endpoint. Defaults to
            ``$OPENAI_EMBEDDING_BASE_URL``, then ``$OPENAI_BASE_URL``, then the
            official API — the ``EMBEDDING``-specific variables exist because
            chat and embeddings often live on different hosts (e.g. chat on
            DeepSeek, which serves no embeddings).
        dimensions: Optional output dimensionality, for models that support
            shortening (e.g. ``text-embedding-3-*``). Part of :attr:`id`.
        client: Optional pre-built :class:`httpx.AsyncClient`. If omitted we
            create one per embedder instance and reuse it.
        timeout: Request timeout in seconds. Defaults to the
            ``LOVIA_PROVIDER_TIMEOUT`` environment variable, else 60.
        default_headers: Extra headers merged into every request.
        trust_env: Whether the embedder-created HTTP client should honor proxy
            and certificate environment variables. Defaults to ``False`` (same
            rationale as the chat providers).
        batch_size: Max texts per request. Kept conservative because
            compatible endpoints commonly cap batches far below OpenAI's.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        dimensions: int | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        default_headers: dict[str, str] | None = None,
        trust_env: bool | None = None,
        batch_size: int = 32,
    ) -> None:
        self.model = model
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_EMBEDDING_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or _DEFAULT_BASE_URL
        ).rstrip("/")
        self.dimensions = dimensions
        self.id = f"openai:{model}:{dimensions}" if dimensions else f"openai:{model}"
        self._api_key = (
            api_key
            or os.environ.get("OPENAI_EMBEDDING_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        self._client = client
        self._owns_client = client is None
        self._timeout = resolve_timeout(timeout)
        self._extra_headers = dict(default_headers or {})
        self._trust_env = resolve_trust_env(trust_env)
        self._batch_size = max(1, batch_size)

    def _check_ready(self) -> None:
        if urlparse(self.base_url).hostname == "api.openai.com" and not self._api_key:
            raise UserError(
                "OpenAIEmbedder requires an API key for api.openai.com",
                hint=(
                    "Set OPENAI_EMBEDDING_API_KEY / OPENAI_API_KEY or pass "
                    "api_key=...; use base_url=... for compatible endpoints "
                    "that do not need one."
                ),
            )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                trust_env=self._trust_env,
                verify=resolve_verify(),
            )
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        for key, value in self._extra_headers.items():
            if key.lower() == "authorization" and self._api_key:
                continue
            headers[key] = value
        return headers

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        self._check_ready()
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            out.extend(await self._embed_batch(texts[start : start + self._batch_size]))
        return out

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"model": self.model, "input": batch}
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        try:
            resp = await self._http().post(
                f"{self.base_url}/embeddings", headers=self._headers(), json=payload
            )
        except httpx.TransportError as exc:
            raise ProviderError(
                f"embeddings request failed: {exc}",
                vendor="openai",
                model=self.model,
                retryable=isinstance(exc, httpx.TimeoutException | httpx.NetworkError),
                hint="Check network connectivity, proxy settings, and base_url.",
            ) from exc
        if resp.status_code >= 400:
            text = resp.text
            raise ProviderError(
                f"embeddings returned HTTP {resp.status_code}: {text}",
                vendor="openai",
                model=self.model,
                status_code=resp.status_code,
                retryable=is_retryable_status(resp.status_code),
                body=text,
            )
        try:
            items = resp.json()["data"]
            # The API may return items out of order; "index" is authoritative.
            vectors = [
                it["embedding"] for it in sorted(items, key=lambda it: it["index"])
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                f"embeddings response is not in OpenAI format: {exc}",
                vendor="openai",
                model=self.model,
            ) from exc
        if len(vectors) != len(batch):
            raise ProviderError(
                f"embeddings returned {len(vectors)} vectors for {len(batch)} inputs",
                vendor="openai",
                model=self.model,
            )
        return vectors


# ---------------------------------------------------------------------------
# Vector math — stdlib only. ``math.sumprod`` (3.12+) gives C-speed dot
# products; the pure-Python fallback keeps 3.10/3.11 correct (a full scan of a
# 10k-doc archive stays well under a second either way).
# ---------------------------------------------------------------------------


def _py_dot(a: Iterable[float], b: Iterable[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


_dot: Any = getattr(math, "sumprod", _py_dot)


def _normalize(v: Sequence[float]) -> array[float]:
    """L2-normalize into a float32 array (so dot product == cosine)."""
    norm = math.sqrt(_dot(v, v))
    if norm == 0.0:
        return array("f", v)
    return array("f", (x / norm for x in v))


# ---------------------------------------------------------------------------
# VectorIndex
# ---------------------------------------------------------------------------

_VEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    id TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    when_ts REAL NOT NULL,
    meta TEXT NOT NULL,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_vectors_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class VectorIndex(Fusable, SQLiteStore):
    """Semantic :class:`~.index.Index`: embeddings in SQLite, cosine search.

    Docs are embedded on ``add`` (one batched call per add) and stored as
    L2-normalized float32 blobs; ``search`` embeds the query and ranks by dot
    product over a full scan. The embedder's :attr:`~Embedder.id` is persisted
    in the file — when it changes, the stored vectors are from a different
    space, so the index resets to empty rather than mixing spaces (the cold
    tier is a recall cache, never the source of truth).
    """

    def __init__(self, path: str | os.PathLike[str], embedder: Embedder) -> None:
        self.embedder = embedder
        p = str(path)
        if p != ":memory:":
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        super().__init__(p, _VEC_SCHEMA)
        self._reset_if_respaced()

    def _reset_if_respaced(self) -> None:
        """Empty the index when the stored vector space no longer matches."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM memory_vectors_meta WHERE key = 'embedder_id'"
            ).fetchone()
            stored = row["value"] if row else None
            if stored == self.embedder.id:
                return
            if stored is not None:
                logger.warning(
                    "memory: embedder changed (%s -> %s); resetting vector index",
                    stored,
                    self.embedder.id,
                )
            conn.execute("DELETE FROM memory_vectors")
            conn.execute(
                "INSERT OR REPLACE INTO memory_vectors_meta VALUES ('embedder_id', ?)",
                (self.embedder.id,),
            )
            conn.commit()
        finally:
            self._release(conn)

    async def add(self, docs: list[Doc]) -> None:
        if not docs:
            return
        vectors = await self.embedder.embed([d.text for d in docs])
        if len(vectors) != len(docs):
            raise ProviderError(
                f"embedder {self.embedder.id!r} returned {len(vectors)} vectors "
                f"for {len(docs)} texts"
            )
        rows = [
            (d.id, d.text, d.when, _dump_meta(d.meta), _normalize(v).tobytes())
            for d, v in zip(docs, vectors)
        ]

        def _impl() -> None:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO memory_vectors "
                    "(id, text, when_ts, meta, vec) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                self._release(conn)

        await self._run(_impl)

    async def remove(self, ids: list[str]) -> None:
        if not ids:
            return

        def _impl() -> None:
            conn = self._connect()
            try:
                conn.executemany(
                    "DELETE FROM memory_vectors WHERE id = ?", [(i,) for i in ids]
                )
                conn.commit()
            finally:
                self._release(conn)

        await self._run(_impl)

    def _is_empty(self) -> bool:
        conn = self._connect()
        try:
            return (
                conn.execute("SELECT 1 FROM memory_vectors LIMIT 1").fetchone() is None
            )
        finally:
            self._release(conn)

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        if not query.strip() or k <= 0:
            return []
        # Don't spend an embedding call on an empty index.
        if await self._run(self._is_empty):
            return []
        (qvec,) = await self.embedder.embed([query])
        q = _normalize(qvec)

        def _scan() -> list[Hit]:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, text, when_ts, meta, vec FROM memory_vectors"
                ).fetchall()
            finally:
                self._release(conn)
            scored = []
            for r in rows:
                v = array("f")
                v.frombytes(r["vec"])
                if len(v) != len(q):
                    # Remnant of another space (misbehaving embedder); skip it.
                    continue
                scored.append((float(_dot(q, v)), r))
            top = nlargest(k, scored, key=operator.itemgetter(0))
            return [_hit(r, score=s) for s, r in top]

        return await self._run(_scan)


__all__ = [
    "Embedder",
    "OpenAIEmbedder",
    "VectorIndex",
]
