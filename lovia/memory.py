"""Long-term memory hook for agents.

``Session`` (in :mod:`lovia.session`) keeps the raw turn-by-turn transcript
for the current conversation. ``Memory`` is the orthogonal hook for
**long-term**, queryable knowledge that may span sessions: vector stores,
summary buffers, fact extraction, RAG indices, etc.

Core ships only the Protocol — no defaults. Pick (or write) an
implementation that matches your storage of choice. A minimal adapter
boils down to two async methods.

Typical wiring patterns:

* **Tool-driven**: expose ``memory.retrieve`` as a tool the model calls
  explicitly. Best when retrieval should be a deliberate decision.
* **Auto-injected**: use :class:`AgentHooks` (or a pre-run step) to call
  ``memory.retrieve`` and prepend results to ``Agent.instructions`` or the
  user message. Best when retrieval should be transparent.

Core deliberately stays out of the way: any auto-injection lives in user
code, not the Runner.
"""

from __future__ import annotations

from typing import Any, Protocol, Sequence


class MemoryRecord(Protocol):
    """A retrieved memory item. Implementations may extend with more fields."""

    content: str
    metadata: dict[str, Any]


class Memory(Protocol):
    """Append-and-query interface for long-term memory.

    The two methods are deliberately minimal. If your backend supports
    deletion, scoping, or hybrid search, expose those as extra methods on
    your concrete class — the Protocol just guarantees the lowest common
    denominator the rest of lovia can rely on.
    """

    async def add(
        self,
        content: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a single memory entry."""
        ...

    async def retrieve(
        self,
        query: str,
        *,
        k: int = 5,
    ) -> Sequence[MemoryRecord]:
        """Return up to ``k`` records relevant to ``query``."""
        ...
