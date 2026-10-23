"""What a served run's tools see as ``RunContext.deps``.

``create_app(run_deps=fn)`` calls ``fn(trigger)`` once per run the server
starts — a chat turn, a resumed run, a scheduled run, a subagent task — and
passes what it returns to the runner as ``deps=``. ``trigger`` says why the
run started and carries two inputs:

* ``client_context`` — the JSON object the page sent with the message
  (extensions add to it with ``window.lovia.provideContext``): UI state, such
  as the knowledge base picked in the composer. The browser controls it, so
  never trust it for who the user is.
* ``request`` — the HTTP request that started the run, after your ``auth=``
  dependency ran: the place for identity (``request.state.user``). ``None``
  for runs nobody requested (schedules, subagents, their reports).

A resumed run (reconnecting after a restart) gets the ``client_context`` of the
run it continues, persisted with the run, and the reconnecting request.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..types import JsonObject, JsonValue

if TYPE_CHECKING:
    from fastapi import Request

    from ..agent import Agent

log = logging.getLogger(__name__)

#: Ceiling on the client context a request may carry, JSON-encoded.
MAX_CLIENT_CONTEXT_BYTES = 64 * 1024

#: The client context of the run on this task — set where runs run
#: (RunSupervisor._run) and read by the schedule_run tool
#: (scheduling.py), so a schedule the agent makes fires in the same context.
RUN_CLIENT: ContextVar[JsonObject | None] = ContextVar("lovia_run_client", default=None)


@dataclass(frozen=True)
class RunTrigger:
    """Why a served run started, handed to ``create_app(run_deps=...)``."""

    session_id: str
    agent: Agent[Any]
    source: str
    """``"user"`` (a chat message, or a reconnect resuming a run),
    ``"schedule:<id>"``, ``"subagent:<session>"`` or ``"subagent-report:<id>"``."""
    request: Request | None = None
    """The request that started the run; ``None`` when nobody requested it."""
    client_context: Mapping[str, JsonValue] = field(default_factory=dict)
    """The page's context for this run — UI state, not identity. (Not
    ``request.client``, which is Starlette's client address.)"""
    resumed: bool = False
    """A reconnect is resuming this run from its checkpoint."""


#: ``create_app(run_deps=...)``: sync or async, returns the run's deps.
RunDepsFn = Callable[[RunTrigger], "Any | Awaitable[Any]"]

_warned_unused = False


async def build_run_deps(fn: RunDepsFn | None, trigger: RunTrigger) -> Any:
    """``fn(trigger)``, awaited if it returns an awaitable; ``None`` without ``fn``."""
    if fn is None:
        global _warned_unused
        if trigger.client_context and not _warned_unused:
            _warned_unused = True
            log.warning(
                "a request carried a client context but create_app has no "
                "run_deps=; it is ignored (set run_deps to use it)"
            )
        return None
    out = fn(trigger)
    if inspect.isawaitable(out):
        out = await out
    return out


def client_context_size(value: Mapping[str, JsonValue]) -> int:
    """Encoded size of a client context, as the size cap measures it."""
    return len(json.dumps(value, ensure_ascii=False).encode())
