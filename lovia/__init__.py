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
    LoviaError,
    MaxTurnsExceeded,
    OutputValidationError,
    ProviderError,
    ToolError,
    UserError,
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
from .runner import RunContext, RunHandle, Runner, RunResult
from .session import Session
from .skills import Skill, SkillCatalog
from .tools import Tool, tool

__all__ = [
    "Agent",
    "AgentHooks",
    "AssistantMessage",
    "ChatMessage",
    "Handoff",
    "LoviaError",
    "MaxTurnsExceeded",
    "ModelSettings",
    "OpenAIChatProvider",
    "OutputValidationError",
    "Provider",
    "ProviderError",
    "RunContext",
    "RunHandle",
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
