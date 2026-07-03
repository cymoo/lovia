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
    InvalidToolArguments,
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
    FileResultStore,
    InMemoryResultStore,
    ResultStore,
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
from .steering import Mailbox
from .run_context import RunContext
from .runtime.result import RunHandle, RunResult
from .runner import Runner
from .session import Segment, Session
from .transcript import TranscriptEntry
from .plugins import (
    Memory,
    OpenAIEmbedder,
    Plugin,
    PluginInstance,
    Skills,
    SkillsError,
    Todo,
)
from .tools import Tool, tool
from .log_config import enable_logging

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
    "FileResultStore",
    "InMemoryResultStore",
    "ResultStore",
    "GuardrailTripped",
    "Handoff",
    "ImagePart",
    "InMemoryCheckpointer",
    "InMemorySession",
    "InvalidToolArguments",
    "TranscriptEntry",
    "LoviaError",
    "Mailbox",
    "MaxTurnsExceeded",
    "MCPError",
    "Memory",
    "ModelSettings",
    "OpenAIChatProvider",
    "OpenAIEmbedder",
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
    "Segment",
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

__version__ = "0.8.4"
