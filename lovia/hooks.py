"""Subscriber-style lifecycle hooks.

`AgentHooks` is a tiny event subscriber: callers attach handlers per event
type with :meth:`on` (or :meth:`on_any`) and the runner dispatches every
emitted event through :meth:`dispatch`.

The previous design exposed one named ``on_*`` method per event type,
which forced users to memorise a wide API and made it awkward to listen
for several event types at once. The subscriber model keeps the surface
small:

* :meth:`on` (``event_type``) — register one handler for one event type
  (or a tuple of types). Usable as a decorator.
* :meth:`on_any` — register a catch-all handler.

Handlers may be sync or async; the dispatcher awaits whichever is
returned. Multiple handlers per event type are supported and called in
registration order.

Typical use::

    hooks = AgentHooks()

    @hooks.on(events.ToolCallStarted)
    async def log_tool(ev):
        print("→", ev.call.name)

    @hooks.on((events.RunCompleted, events.ErrorOccurred))
    def at_end(ev):
        print("end:", type(ev).__name__)

    agent = Agent(..., hooks=hooks)
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, TypeVar, Union

from . import events

logger = logging.getLogger(__name__)

# A handler may be sync or async; both shapes are supported.
Handler = Callable[[Any], Union[None, Awaitable[None]]]

E = TypeVar("E", bound=events.Event)


class AgentHooks:
    """Collection of event subscribers."""

    def __init__(self) -> None:
        # Mapping from event type → list of registered handlers. We keep
        # one bucket per concrete event class; ``dispatch`` walks them and
        # uses ``isinstance`` to support subclass matching.
        self._listeners: dict[type, list[Handler]] = {}
        self._any: list[Handler] = []

    def on(
        self,
        event_type: "type[E] | tuple[type[E], ...]",
    ) -> Callable[[Handler], Handler]:
        """Register a handler for one event type or a tuple of types.

        Returns the original function so it can be used as a decorator.
        """
        types = event_type if isinstance(event_type, tuple) else (event_type,)

        def decorator(fn: Handler) -> Handler:
            for t in types:
                self._listeners.setdefault(t, []).append(fn)
            return fn

        return decorator

    def on_any(self, fn: Handler) -> Handler:
        """Register a catch-all handler invoked for every event."""
        self._any.append(fn)
        return fn

    async def dispatch(self, event: events.Event) -> None:
        """Invoke every matching handler for ``event``.

        Handler exceptions are logged and swallowed: hooks are observers, and
        a broken log/metrics handler must not abort the run it watches.
        """
        # First the catch-alls so listeners that mutate state see events
        # in the same order the runner emits them.
        for fn in self._any:
            await _call_handler(fn, event)
        for event_type, listeners in self._listeners.items():
            if isinstance(event, event_type):
                for fn in listeners:
                    await _call_handler(fn, event)


async def _call_handler(fn: Handler, event: events.Event) -> None:
    """Run one handler (sync or async), logging instead of raising on failure."""
    try:
        result = fn(event)
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception:
        logger.exception(
            "hook handler %r failed for %s; continuing",
            getattr(fn, "__qualname__", fn),
            type(event).__name__,
        )


async def dispatch(hooks: AgentHooks | None, event: events.Event) -> None:
    """Convenience entry point used by the runner."""
    if hooks is None:
        return
    await hooks.dispatch(event)
