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
from .guardrails import (
    InputGuardrail,
    OutputGuardrail,
)
from .handoff import Handoff, agent_as_tool
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
    ArchiveHit,
    FileNotesStore,
    Memory,
    MemoryArchive,
    NotesStore,
    Plugin,
    PluginInstance,
    SQLiteMemoryArchive,
    Skill,
    Skills,
    SkillsError,
    Todo,
    TodoItem,
    TodoList,
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
) -> _logging.Logger:
    """Configure the ``lovia`` logger for quick interactive use.

    Convenience for scripts and notebooks. Attaches a single
    :class:`~logging.StreamHandler` to the ``lovia`` logger with a sensible
    default format and sets its level. Idempotent — calling more than once
    replaces the previously attached handler so log lines aren't duplicated.

    For production deployments configure :mod:`logging` yourself; nothing in
    ``lovia`` calls this function automatically.

    Args:
        level: Logger level (e.g. ``logging.DEBUG``, ``"INFO"``).
        format: ``logging`` format string.
        datefmt: ``logging`` date format string.
        stream: Optional stream override (defaults to ``sys.stderr``).

    Returns:
        The configured ``lovia`` logger.
    """
    log = _logging.getLogger("lovia")
    # Strip handlers we've previously attached so successive calls don't pile
    # up duplicate StreamHandlers — but keep the NullHandler so logging stays
    # well-behaved if the user later calls ``logging.disable`` etc.
    for h in list(log.handlers):
        if getattr(h, "_lovia_managed", False):
            log.removeHandler(h)
    handler = _logging.StreamHandler(stream)
    handler.setFormatter(_logging.Formatter(format, datefmt=datefmt))
    handler._lovia_managed = True  # type: ignore[attr-defined]
    log.addHandler(handler)
    log.setLevel(level)
    return log


__all__ = [
    "Agent",
    "AgentHooks",
    "AnthropicProvider",
    "ArchiveHit",
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
    "InputGuardrail",
    "TranscriptEntry",
    "LoviaError",
    "MaxTurnsExceeded",
    "MCPError",
    "Memory",
    "MemoryArchive",
    "ModelSettings",
    "OpenAIChatProvider",
    "NotesStore",
    "OutputGuardrail",
    "FileNotesStore",
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
    "SQLiteMemoryArchive",
    "SQLiteSession",
    "Session",
    "Skill",
    "Skills",
    "SkillsError",
    "TextPart",
    "Todo",
    "TodoItem",
    "TodoList",
    "Tool",
    "ToolError",
    "Usage",
    "UserError",
    "agent_as_tool",
    "assistant",
    "enable_logging",
    "events",
    "system",
    "tool",
    "user",
]

__version__ = "0.6.14"
