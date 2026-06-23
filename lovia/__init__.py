"""lovia - a lightweight and elegant async agent framework.

Typical use::

    from lovia import Agent, Runner, tool

    @tool
    async def add(a: int, b: int) -> int:
        '''Add two integers.'''
        return a + b

    agent = Agent(
        name="calc",
        instructions="You are a helpful calculator.",
        model="openai:gpt-5.4",
        tools=[add],
    )

    import asyncio
    result = asyncio.run(Runner.run(agent, "What is 2 + 3?"))
    print(result.output)
"""

from __future__ import annotations

import logging as _logging
from typing import TextIO

from . import events
from .agent import Agent
from .checkpointer import CheckpointOptions
from .stores import (
    InMemoryCheckpointer,
    InMemorySession,
    SQLiteCheckpointer,
    SQLiteSession,
)
from .parts import (
    FilePart,
    ImagePart,
    TextPart,
)
from .exceptions import (
    BudgetExceeded,
    ContextOverflowError,
    GuardrailTripped,
    LoviaError,
    MaxTurnsExceeded,
    MCPError,
    OutputValidationError,
    ProviderError,
    RunCancelled,
    ToolError,
    UserError,
)
from .context import (
    Compaction,
    ContextPolicy,
)
from .handoff import Handoff
from .hooks import AgentHooks
from .messages import (
    Message,
    Usage,
    assistant,
    system,
    user,
)
from .providers import (
    AnthropicProvider,
    ModelSettings,
    OpenAIChatProvider,
    Provider,
)
from .reliability import CancelToken, RetryPolicy, RunBudget
from .run_context import RunContext
from .runtime.result import RunHandle, RunResult
from .runner import Runner
from .session import Session
from .transcript import TranscriptEntry
from .plugins import (
    Memory,
    Plugin,
    PluginInstance,
    Skills,
    SkillsError,
    Todo,
)
from .tools import Tool, tool

# ---------------------------------------------------------------------------
# Logging setup
#
# Library best practice: attach a NullHandler so applications that don't
# configure logging don't see ``No handlers could be found`` warnings, but
# also don't get unsolicited output. Users opt in via ``logging.basicConfig``
# or :func:`enable_logging`.
# ---------------------------------------------------------------------------
_logging.getLogger("lovia").addHandler(_logging.NullHandler())


def enable_logging(
    level: int | str = _logging.INFO,
    *,
    format: str = "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt: str = "%H:%M:%S",
    stream: TextIO | None = None,
    propagate: bool = False,
) -> _logging.Logger:
    """Configure the ``lovia`` logger for quick interactive use.

    Convenience for scripts and notebooks. Attaches a single
    :class:`~logging.StreamHandler` to the ``lovia`` logger with a sensible
    default format and sets its level. Idempotent — calling more than once
    replaces the previously attached handler so log lines aren't duplicated.

    By default the ``lovia`` logger's :attr:`~logging.Logger.propagate` is set
    to ``False`` so records aren't *also* emitted by the root logger — which
    would double-print whenever the app has configured root logging (e.g. via
    :func:`logging.basicConfig` or under uvicorn). Pass ``propagate=True`` to
    keep propagating to ancestor handlers as well.

    For production deployments configure :mod:`logging` yourself; nothing in
    ``lovia`` calls this function automatically.

    Args:
        level: Logger level (e.g. ``logging.DEBUG``, ``"INFO"``).
        format: ``logging`` format string.
        datefmt: ``logging`` date format string.
        stream: Optional stream override (defaults to ``sys.stderr``).
        propagate: Whether the ``lovia`` logger should also forward records to
            ancestor (root) handlers. Defaults to ``False`` to avoid duplicate
            output.

    Returns:
        The configured ``lovia`` logger.
    """
    log = _logging.getLogger("lovia")
    # Strip only the handlers we attached on a previous call, so successive
    # calls don't pile up duplicate StreamHandlers. The NullHandler and any
    # handlers the user added themselves are left untouched.
    for h in list(log.handlers):
        if getattr(h, "_lovia_managed", False):
            log.removeHandler(h)
    handler = _logging.StreamHandler(stream)
    handler.setFormatter(_logging.Formatter(format, datefmt=datefmt))
    handler._lovia_managed = True  # type: ignore[attr-defined]
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = propagate
    return log


__all__ = [
    "Agent",
    "AgentHooks",
    "AnthropicProvider",
    "BudgetExceeded",
    "CancelToken",
    "Message",
    "CheckpointOptions",
    "ContextOverflowError",
    "Compaction",
    "ContextPolicy",
    "GuardrailTripped",
    "Handoff",
    "ImagePart",
    "InMemoryCheckpointer",
    "InMemorySession",
    "TranscriptEntry",
    "LoviaError",
    "MaxTurnsExceeded",
    "MCPError",
    "Memory",
    "ModelSettings",
    "OpenAIChatProvider",
    "FilePart",
    "OutputValidationError",
    "Plugin",
    "PluginInstance",
    "Provider",
    "ProviderError",
    "RetryPolicy",
    "RunBudget",
    "RunCancelled",
    "RunContext",
    "RunHandle",
    "RunResult",
    "Runner",
    "SQLiteCheckpointer",
    "SQLiteSession",
    "Session",
    "Skills",
    "SkillsError",
    "TextPart",
    "Todo",
    "Tool",
    "ToolError",
    "Usage",
    "UserError",
    "assistant",
    "enable_logging",
    "events",
    "system",
    "tool",
    "user",
]

__version__ = "0.7.2"
