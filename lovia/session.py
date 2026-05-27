"""Session protocol.

A :class:`Session` stores the message history for a conversation. It is an
intentionally minimal async protocol; concrete implementations live in
:mod:`lovia.stores`.

The runner accepts an optional ``Session``; if provided, it loads the prior
messages, prepends them to the input, and persists new messages at the end of
the run. Application code controls the ``session_id`` (so multi-user systems
just key sessions by user/conversation id).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .messages import ChatMessage


@runtime_checkable
class Session(Protocol):
    """A conversation transcript store keyed by ``session_id``."""

    async def load(self, session_id: str) -> list[ChatMessage]: ...

    async def append(self, session_id: str, messages: list[ChatMessage]) -> None: ...

    async def clear(self, session_id: str) -> None: ...
