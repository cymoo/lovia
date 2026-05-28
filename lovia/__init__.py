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
        model="openai:gpt-4o-mini",
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
from .content import ContentBlock, ImageBlock, TextBlock
from .exceptions import (
    BudgetExceeded,
    ContextOverflowError,
    GuardrailTripped,
    LoviaError,
    MaxTurnsExceeded,
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
    AssistantMessage,
    ChatMessage,
    ToolCall,
    Usage,
    assistant,
    system,
    user,
)
from .providers import ModelSettings, OpenAIChatProvider, Provider, provider_from_string
from .providers.openai_responses import OpenAIResponsesProvider
from .reliability import CancelToken, RetryPolicy, RunBudget
from .runner import RunContext, RunHandle, Runner, RunResult
from .sandbox import (
    AuditPolicy,
    AuditRecord,
    AuditStream,
    AuditToolPolicy,
    DirEntry,
    ExecLimits,
    ExecResult,
    LocalSandbox,
    LocalSandboxProvider,
    Sandbox,
    SandboxProvider,
    attach_sandbox,
    default_audit_policy,
    pass_through_policy,
    sandbox_tools,
)
from .session import Session
from .skills import Skill, SkillCatalog
from .memory import Memory
from .items import (
    FinishDelta,
    InputMessageItem,
    Item,
    ItemDelta,
    MessageOutputItem,
    ReasoningDelta,
    ReasoningItem,
    TextDelta,
    ToolCallDelta,
    ToolCallItem,
    ToolCallOutputItem,
    UsageDelta,
    assistant_to_items,
    input_to_items,
    item_from_dict,
    item_to_dict,
    items_to_chat_messages,
    safe_window,
    transcript_to_items,
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
    "ApprovalChannel",
    "ArchiveCallback",
    "ArchiveEvent",
    "AssistantMessage",
    "BudgetExceeded",
    "CancelToken",
    "ChatMessage",
    "Checkpointer",
    "AuditPolicy",
    "AuditRecord",
    "AuditStream",
    "AuditToolPolicy",
    "ConsoleTracer",
    "ContentBlock",
    "ContextOverflowError",
    "ContextPolicy",
    "DEFAULT_SUMMARY_PROMPT",
    "DirEntry",
    "ExecLimits",
    "ExecResult",
    "GuardrailFn",
    "GuardrailTripped",
    "Handoff",
    "ImageBlock",
    "InMemoryCheckpointer",
    "InMemoryTracer",
    "InputGuardrail",
    "InputMessageItem",
    "Item",
    "ItemDelta",
    "LoviaError",
    "MaxTurnsExceeded",
    "Memory",
    "MessageOutputItem",
    "ModelSettings",
    "NoopContextPolicy",
    "NoopTracer",
    "OpenAIChatProvider",
    "OpenAIResponsesProvider",
    "OutputGuardrail",
    "OutputRepairStrategy",
    "DefaultOutputRepair",
    "OutputValidationError",
    "PolicyContext",
    "Provider",
    "ProviderError",
    "ProviderSummarizer",
    "ReasoningDelta",
    "ReasoningItem",
    "RetryPolicy",
    "RunBudget",
    "RunCancelled",
    "RunContext",
    "RunHandle",
    "RunResult",
    "RunSnapshot",
    "Runner",
    "Sandbox",
    "SandboxProvider",
    "Session",
    "Skill",
    "SkillCatalog",
    "LocalSandbox",
    "LocalSandboxProvider",
    "Summarizer",
    "SummarizingContextPolicy",
    "TextBlock",
    "TextDelta",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
    "ToolCallItem",
    "ToolCallOutputItem",
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
    "assistant_to_items",
    "attach_sandbox",
    "default_audit_policy",
    "drop_stale_tool_calls",
    "enable_logging",
    "events",
    "input_to_items",
    "item_from_dict",
    "item_to_dict",
    "items_to_chat_messages",
    "pass_through_policy",
    "provider_from_string",
    "safe_window",
    "sandbox_tools",
    "transcript_to_items",
    "system",
    "tool",
    "user",
]

__version__ = "0.1.0"
