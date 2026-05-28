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
    "ConsoleTracer",
    "ContentBlock",
    "ContextOverflowError",
    "ContextPolicy",
    "DEFAULT_SUMMARY_PROMPT",
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
    "Session",
    "Skill",
    "SkillCatalog",
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
    "drop_stale_tool_calls",
    "events",
    "input_to_items",
    "item_from_dict",
    "item_to_dict",
    "items_to_chat_messages",
    "provider_from_string",
    "safe_window",
    "transcript_to_items",
    "system",
    "tool",
    "user",
]

__version__ = "0.1.0"
