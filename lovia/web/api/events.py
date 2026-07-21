"""The process-wide lifecycle stream: ``GET /api/events``.

One SSE connection per client replaces polling ``/api/runs`` + the session
list: the frontend subscribes once and updates the sidebar, notifications, and
schedule status reactively. Events are small JSON facts published via
:meth:`RouterDeps.emit <lovia.web.api.deps.RouterDeps.emit>`:

* ``run_started`` / ``run_finished`` — ``{session_id, run_id, agent?, source,
  status?, error?}``; terminal outcomes carry the run-record status
  (``completed | failed | cancelled | interrupted``).
* ``session_created`` / ``session_retitled`` — ``{session_id, agent?, title}``.

No replay: a (re)connecting client fetches one snapshot (``/api/sessions`` +
``/api/runs``) and then trusts the stream — anything missed while disconnected
is covered by that snapshot, which is also how an overflowed (too-slow)
subscriber recovers after this stream closes on it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

try:
    from fastapi import APIRouter
    from sse_starlette.sse import EventSourceResponse
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..supervisor import _Overflow
from .deps import RouterDeps


def build_events_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/events")
    async def events_stream() -> EventSourceResponse:
        async def stream() -> AsyncIterator[dict[str, str]]:
            sub = deps.bus.subscribe()
            try:
                async for _seq, payload in sub:
                    # The bus payload type is opaque; only emit()'s SSE dicts
                    # belong on the wire — anything else must not kill the
                    # stream for every connected client.
                    if isinstance(payload, dict):
                        yield payload
            except _Overflow:
                # Fell behind → end the stream; EventSource auto-reconnects
                # and the client's on-open snapshot refetch closes the gap.
                return
            finally:
                sub.close()

        return EventSourceResponse(stream())

    return router
