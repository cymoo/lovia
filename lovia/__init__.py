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
from .checkpointer import Checkpointer, InMemoryCheckpointer, RunSnapshot
from .content import ContentBlock, ImageBlock, TextBlock
from .exceptions import (
    BudgetExceeded,
    GuardrailTripped,
    LoviaError,
    MaxTurnsExceeded,
    OutputValidationError,
    ProviderError,
    RunCancelled,
    ToolError,
    UserError,
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
from .reliability import CancelToken, RetryPolicy, RunBudget
from .runner import RunContext, RunHandle, Runner, RunResult
from .session import Session
from .skills import Skill, SkillCatalog
from .tools import Tool, tool
from .tracing import ConsoleTracer, InMemoryTracer, NoopTracer, Tracer

__all__ = [
    "Agent",
    "AgentHooks",
    "AssistantMessage",
    "BudgetExceeded",
    "CancelToken",
    "ChatMessage",
    "Checkpointer",
    "ConsoleTracer",
    "ContentBlock",
    "GuardrailFn",
    "GuardrailTripped",
    "Handoff",
    "ImageBlock",
    "InMemoryCheckpointer",
    "InMemoryTracer",
    "InputGuardrail",
    "LoviaError",
    "MaxTurnsExceeded",
    "ModelSettings",
    "NoopTracer",
    "OpenAIChatProvider",
    "OutputGuardrail",
    "OutputValidationError",
    "Provider",
    "ProviderError",
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
    "TextBlock",
    "Tool",
    "ToolCall",
    "ToolError",
    "Tracer",
    "Usage",
    "UserError",
    "agent_as_tool",
    "assistant",
    "drop_stale_tool_calls",
    "events",
    "provider_from_string",
    "system",
    "tool",
    "user",
]

__version__ = "0.1.0"
