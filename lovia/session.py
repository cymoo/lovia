"""Session protocol.

A :class:`Session` stores the conversation history for a multi-turn chat. It
is an intentionally minimal async protocol; concrete implementations live in
:mod:`lovia.stores`.

The runner accepts an optional ``Session``; if provided, it loads the prior
items, converts them to the wire format for the model, and appends the new
items produced by the run when it finishes. Application code controls the
``session_id`` so multi-user systems just key sessions by user / conversation
id.

Why :class:`Item` and not :class:`ChatMessage`?
The Item form is richer (it preserves reasoning, server-side tool calls,
and provider-specific metadata) and round-trips losslessly to the OpenAI
Responses API. Adapters that only speak Chat Completions can still flatten
items via :func:`lovia.items.items_to_chat_messages`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .items import Item


@runtime_checkable
class Session(Protocol):
    """A conversation transcript store keyed by ``session_id``."""

    async def load(self, session_id: str) -> list[Item]: ...

    async def append(self, session_id: str, items: list[Item]) -> None: ...

    async def clear(self, session_id: str) -> None: ...
