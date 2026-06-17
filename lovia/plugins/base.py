"""Plugin protocol: declarative, additive capability bundles for an Agent.

A :class:`Plugin` is a **declarative** feature attached to an :class:`Agent`.
The runner activates it **once per run** (via the async :meth:`Plugin.setup`),
so a plugin that needs run-scoped state simply builds a fresh store in ``setup``
and closes its tools/injectors over it — concurrency-safe by construction, the
same way ``workspace`` produces a fresh session per run. Keep run state inside
``setup``, not on the plugin object, or concurrent runs of one agent would
share it.

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
* ``input_guardrails`` / ``output_guardrails`` — guardrail callables the runner
  runs at its existing input/output checkpoints, merged with the agent's own.
  The runner — never the plugin — owns the abort, so a self-contained feature
  (e.g. PII redaction) can ship its checks alongside its tools and instructions.

The instance also carries an ``aclose`` coroutine the runner invokes (LIFO, best
effort) when the run ends, so a plugin that opens a resource in ``setup`` (an MCP
connection, an HTTP client) tears it down cleanly.

See :class:`lovia.plugins.Todo` for the first concrete plugin.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol

if TYPE_CHECKING:
    from ..guardrails import GuardrailFn
    from ..hooks import AgentHooks
    from ..run_context import RunContext
    from ..tools import Tool
    from ..transcript import TranscriptEntry


# A view injector is evaluated once per turn with the live run context. It
# returns transient entries to append to the tail of that turn's model view, or
# ``None`` to inject nothing. May be sync or async. A raising injector is
# logged and skipped — the model call still proceeds.
ViewInjector = Callable[
    ["RunContext[Any]"],
    "list[TranscriptEntry] | None | Awaitable[list[TranscriptEntry] | None]",
]


async def _noop_aclose() -> None:
    """Default teardown for plugins that hold no run-scoped resources."""
    return None


@dataclass
class PluginInstance:
    """The per-run contributions produced by :meth:`Plugin.setup`.

    Every field is optional: a plugin contributes any subset. Stateful plugins
    build their store in ``setup`` and close ``tools``/``view_injectors`` over
    it so all contributions share the same instance. Set ``aclose`` to a
    coroutine to release resources opened during ``setup``.
    """

    tools: "list[Tool]" = field(default_factory=list)
    view_injectors: list[ViewInjector] = field(default_factory=list)
    instructions: str | None = None
    hooks: "AgentHooks | None" = None
    input_guardrails: "list[GuardrailFn]" = field(default_factory=list)
    output_guardrails: "list[GuardrailFn]" = field(default_factory=list)
    aclose: Callable[[], Awaitable[None]] = _noop_aclose


class Plugin(Protocol):
    """A declarative feature that contributes capabilities to an agent run.

    ``setup`` is awaited once per run (and once per agent on a handoff) and must
    return a fresh :class:`PluginInstance`; the runner never shares one across
    runs, so run-scoped state created inside ``setup`` stays isolated per run.
    It is ``async`` so a plugin may open resources (e.g. an MCP connection)
    during activation; pair that with :attr:`PluginInstance.aclose` for
    teardown.
    """

    name: str

    async def setup(self) -> PluginInstance: ...


__all__ = ["Plugin", "PluginInstance", "ViewInjector"]
