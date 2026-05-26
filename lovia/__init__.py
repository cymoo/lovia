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
from .exceptions import (
    ApprovalDenied,
    LoviaError,
    MaxTurnsExceeded,
    OutputValidationError,
    ProviderError,
    ToolError,
    UserError,
)
from .handoff import Handoff
from .hooks import AgentHooks
from .messages import AssistantMessage, ChatMessage, ToolCall, Usage, assistant, system, user
from .providers import ModelSettings, OpenAIChatProvider, Provider, provider_from_string
from .runner import RunContext, Runner, RunResult
from .session import MemoryStore, Session
from .skills import Skill, SkillCatalog
from .tools import Tool, tool

__all__ = [
    "Agent",
    "AgentHooks",
    "ApprovalDenied",
    "AssistantMessage",
    "ChatMessage",
    "Handoff",
    "LoviaError",
    "MaxTurnsExceeded",
    "MemoryStore",
    "ModelSettings",
    "OpenAIChatProvider",
    "OutputValidationError",
    "Provider",
    "ProviderError",
    "RunContext",
    "RunResult",
    "Runner",
    "Session",
    "Skill",
    "SkillCatalog",
    "Tool",
    "ToolCall",
    "ToolError",
    "Usage",
    "UserError",
    "assistant",
    "events",
    "provider_from_string",
    "system",
    "tool",
    "user",
]

__version__ = "0.1.0"
