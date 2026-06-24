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

Every handler is called as ``handler(event, ctx)``: it receives the **event**
and the run's live :class:`~lovia.run_context.RunContext` — the run's dynamic
state (``session_id``, the active ``agent``, cumulative ``usage``, the live
transcript, the ``cancel_token``). This mirrors guardrails and view-injectors,
which already always receive the context. A handler that only cares about the
event simply ignores ``ctx``. Handlers may be sync or async; the dispatcher
awaits whichever is returned. Multiple handlers per event type are supported and
called in registration order.

The handler's event parameter is checked against the registered type: a handler
attached with ``on(ToolCallStarted)`` may annotate it ``ev: ToolCallStarted``
and get both autocomplete and a static error if it is wired to the wrong event.

Typical use::

    hooks = AgentHooks()

    @hooks.on(events.ToolCallStarted)
    async def log_tool(ev: events.ToolCallStarted, ctx: RunContext):
        print("→", ev.call.name, "in session", ctx.session_id)

    @hooks.on((events.RunCompleted, events.ErrorOccurred))
    def at_end(ev, ctx):
        print("end:", type(ev).__name__)

    agent = Agent(..., hooks=hooks)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar, Union

from . import events

if TYPE_CHECKING:
    from .run_context import RunContext

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=events.Event)

# A handler receives the event and the run's live RunContext; either may be
# sync or async. ``HookHandler[E]`` narrows the event to the registered type so
# ``def h(ev: ToolCallStarted, ctx)`` is checked against ``on(ToolCallStarted)``.
HookHandler = Callable[[E, "RunContext[Any]"], Union[None, Awaitable[None]]]


class AgentHooks:
    """Collection of event subscribers."""

    def __init__(self) -> None:
        # Mapping from event type → list of registered handlers. We keep
        # one bucket per concrete event class; ``dispatch`` walks them and
        # uses ``isinstance`` to support subclass matching.
        self._listeners: dict[type, list[HookHandler[Any]]] = {}
        self._any: list[HookHandler[events.Event]] = []

    def on(
        self,
        event_type: "type[E] | tuple[type[E], ...]",
    ) -> "Callable[[HookHandler[E]], HookHandler[E]]":
        """Register a handler for one event type or a tuple of types.

        The handler is called as ``handler(event, ctx)``. Returns the original
        function so it can be used as a decorator.
        """
        types = event_type if isinstance(event_type, tuple) else (event_type,)

        def decorator(fn: "HookHandler[E]") -> "HookHandler[E]":
            for t in types:
                self._listeners.setdefault(t, []).append(fn)
            return fn

        return decorator

    def on_any(
        self, fn: "HookHandler[events.Event]"
    ) -> "HookHandler[events.Event]":
        """Register a catch-all handler, called as ``handler(event, ctx)`` for
        every event."""
        self._any.append(fn)
        return fn

    async def dispatch(self, event: events.Event, ctx: "RunContext[Any]") -> None:
        """Invoke every matching handler for ``event`` as ``handler(event, ctx)``.

        Handler exceptions are logged and swallowed: hooks are observers, and a
        broken log/metrics handler must not abort the run it watches.
        """
        # First the catch-alls so listeners that mutate state see events
        # in the same order the runner emits them.
        for fn in self._any:
            await _call_handler(fn, event, ctx)
        for event_type, listeners in self._listeners.items():
            if isinstance(event, event_type):
                for fn in listeners:
                    await _call_handler(fn, event, ctx)


async def _call_handler(
    fn: "HookHandler[Any]", event: events.Event, ctx: "RunContext[Any]"
) -> None:
    """Run one handler (sync or async), logging instead of raising on failure."""
    try:
        result = fn(event, ctx)
        if result is not None and hasattr(result, "__await__"):
            await result
    except Exception:
        # Fail-open: a crashing user hook must not abort the run, so this is a
        # WARNING (the run continues correctly) rather than an ERROR — but keep
        # the traceback via exc_info.
        logger.warning(
            "hook.error: handler %r failed for %s; continuing",
            getattr(fn, "__qualname__", fn),
            type(event).__name__,
            exc_info=True,
        )


async def dispatch(
    hooks: AgentHooks | None, event: events.Event, ctx: "RunContext[Any]"
) -> None:
    """Convenience entry point used by the runner."""
    if hooks is None:
        return
    await hooks.dispatch(event, ctx)
