"""Plugins: bundle a feature's tools, view injectors, instructions, and hooks.

A :class:`Plugin` is a **declarative** feature attached to an :class:`Agent`.
The runner activates it **once per run** (via :meth:`Plugin.setup`), so a plugin
that needs run-scoped state simply builds a fresh store in ``setup`` and closes
its tools/injectors over it — concurrency-safe by construction, the same way
``workspace`` produces a fresh session per run. Keep run state inside ``setup``,
not on the plugin object, or concurrent runs of one agent would share it.

A plugin contributes capabilities across the agent's existing extension axes via
:class:`PluginInstance`:

* ``tools`` — merged into the agent's tool set (one namespace; name clashes are
  reported like any other tool source).
* ``view_injectors`` — callables evaluated **every turn** that append transient
  entries to the tail of the per-call model view. The injected entries are used
  for that one call only: never written to the transcript or the session, so
  they neither accumulate as turns grow nor bust the cached system-prompt
  prefix. This is the mechanism behind "always re-show the current todos".
* ``instructions`` — a static string appended to the system prompt (rendered
  once at run start, like the agent's own instructions and ``workspace`` /
  ``skills`` instructions).
* ``hooks`` — an :class:`~lovia.hooks.AgentHooks` whose handlers receive every
  run event, dispatched alongside the agent's own hooks. Lets a plugin observe
  the run (metrics, audit, notifications) without a second mechanism.

See :func:`lovia.todos.todo_plugin` for the first concrete plugin.

Out of scope for v1 (non-breaking to add later): async ``setup``/teardown for
plugins that acquire resources, and a channel for plugins to *emit* custom
events into the run stream (today, surface state changes by observing
``ToolCallCompleted`` or via ``hooks``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .hooks import AgentHooks
    from .run_context import RunContext
    from .tools import Tool
    from .transcript import TranscriptEntry


# A view injector is evaluated once per turn with the live run context. It
# returns transient entries to append to the tail of that turn's model view, or
# ``None`` to inject nothing. May be sync or async. A raising injector is
# logged and skipped — the model call still proceeds.
ViewInjector = Callable[
    ["RunContext"],
    "list[TranscriptEntry] | None | Awaitable[list[TranscriptEntry] | None]",
]


@dataclass
class PluginInstance:
    """The per-run contributions produced by :meth:`Plugin.setup`.

    Every field is optional: a plugin contributes any subset. Stateful plugins
    build their store in ``setup`` and close ``tools``/``view_injectors`` over
    it so all contributions share the same instance.
    """

    tools: "list[Tool]" = field(default_factory=list)
    view_injectors: list[ViewInjector] = field(default_factory=list)
    instructions: str | None = None
    hooks: "AgentHooks | None" = None


@runtime_checkable
class Plugin(Protocol):
    """A declarative feature that contributes capabilities to an agent run.

    ``setup`` is called once per run (and once per agent on a handoff) and must
    return a fresh :class:`PluginInstance`; the runner never shares one across
    runs, so run-scoped state created inside ``setup`` stays isolated per run.
    """

    name: str

    def setup(self) -> PluginInstance: ...


__all__ = ["Plugin", "PluginInstance", "ViewInjector"]
