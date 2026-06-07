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
from typing import Any as _Any

from . import events
from .agent import Agent
from .approvals import ApprovalChannel
from .checkpointer import Checkpointer, InMemoryCheckpointer, RunSnapshot
from .content import (
    ContentPart,
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
from .context_policy import (
    ArchiveCallback,
    ArchiveEvent,
    ContextPolicy,
    DEFAULT_SUMMARY_PROMPT,
    NoopContextPolicy,
    PolicyContext,
    ProviderSummarizer,
    Summarizer,
    SummarizingContextPolicy,
)
from .guardrails import (
    GuardrailFn,
    InputGuardrail,
    OutputGuardrail,
)
from .handoff import Handoff, agent_as_tool, drop_stale_tool_calls
from .hooks import AgentHooks
from .messages import (
    AssistantTurn,
    Message,
    ToolCall,
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
    provider_from_string,
)
from .reliability import CancelToken, RetryPolicy, RunBudget
from .run_context import RunContext
from .runtime.result import RunHandle, RunResult
from .runner import Runner
from .session import Session
from .skills import (
    LocalDirSkillSource,
    Skill,
    SkillFilter,
    SkillMetadata,
    SkillSource,
    Skills,
    SkillsError,
)
from .memory import Memory
from .transcript import (
    FinishDelta,
    InputEntry,
    TranscriptEntry,
    EntryCompletedDelta,
    ModelDelta,
    AssistantTextEntry,
    ReasoningDelta,
    ReasoningEntry,
    TextDelta,
    ToolCallDelta,
    ToolCallEntry,
    ToolResultEntry,
    UsageDelta,
    assistant_to_entries,
    input_to_entries,
    entry_from_dict,
    entry_to_dict,
    entries_to_messages,
    safe_window,
    messages_to_entries,
)
from .output import DefaultOutputRepair, OutputRepairStrategy
from .tools import Tool, ToolPolicy, ToolResultRenderer, ToolWrap, WrapPolicy, tool
from .tracing import ConsoleTracer, InMemoryTracer, NoopTracer, Tracer

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
    stream: _Any = None,
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
    "ApprovalChannel",
    "ArchiveCallback",
    "ArchiveEvent",
    "AssistantTurn",
    "BudgetExceeded",
    "CancelToken",
    "Message",
    "Checkpointer",
    "ConsoleTracer",
    "ContentPart",
    "ContextOverflowError",
    "ContextPolicy",
    "DEFAULT_SUMMARY_PROMPT",
    "GuardrailFn",
    "GuardrailTripped",
    "Handoff",
    "ImagePart",
    "InMemoryCheckpointer",
    "InMemoryTracer",
    "InputGuardrail",
    "InputEntry",
    "TranscriptEntry",
    "EntryCompletedDelta",
    "ModelDelta",
    "LoviaError",
    "MaxTurnsExceeded",
    "MCPError",
    "Memory",
    "AssistantTextEntry",
    "ModelSettings",
    "NoopContextPolicy",
    "NoopTracer",
    "OpenAIChatProvider",
    "OutputGuardrail",
    "OutputRepairStrategy",
    "DefaultOutputRepair",
    "FilePart",
    "OutputValidationError",
    "PolicyContext",
    "Provider",
    "ProviderError",
    "ProviderSummarizer",
    "ReasoningDelta",
    "ReasoningEntry",
    "RetryPolicy",
    "RunBudget",
    "RunCancelled",
    "RunContext",
    "RunHandle",
    "RunResult",
    "RunSnapshot",
    "Runner",
    "Session",
    "Skill",
    "SkillMetadata",
    "SkillSource",
    "SkillFilter",
    "Skills",
    "SkillsError",
    "LocalDirSkillSource",
    "Summarizer",
    "SummarizingContextPolicy",
    "TextPart",
    "TextDelta",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
    "ToolCallEntry",
    "ToolResultEntry",
    "ToolError",
    "ToolPolicy",
    "ToolResultRenderer",
    "ToolWrap",
    "Tracer",
    "Usage",
    "UsageDelta",
    "FinishDelta",
    "UserError",
    "WrapPolicy",
    "agent_as_tool",
    "assistant",
    "assistant_to_entries",
    "drop_stale_tool_calls",
    "enable_logging",
    "events",
    "input_to_entries",
    "entry_from_dict",
    "entry_to_dict",
    "entries_to_messages",
    "provider_from_string",
    "safe_window",
    "messages_to_entries",
    "system",
    "tool",
    "user",
]

__version__ = "0.5.10"
