"""Serve an agent over HTTP with a built-in chat UI.

Run::

    pip install -e .[web]
    python examples/16_web_serve.py
    # open http://127.0.0.1:8000

Demonstrates the optional ``lovia.web`` layer: REST endpoints, SSE streaming,
human-in-the-loop approval, the bundled refined-minimal chat page, and
``Compaction`` so a long chat session never crashes the provider with a
context-window overflow.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from lovia import Agent, Compaction, Todo, tool, enable_logging
from lovia.workspace import Workspace
from lovia.web import serve

load_dotenv()


@tool
async def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@tool(needs_approval=True)
async def send_email(to: str, subject: str, body: str) -> str:
    """Send an email — requires the user's approval in the UI."""
    return f"sent to {to}: {subject!r}"


def main() -> None:
    enable_logging()

    agent = Agent(
        name="lovia",
        instructions=(
            "You are a friendly assistant. Use tools when helpful. "
            "Keep replies short and conversational. Ask for clarification if the question is ambiguous."
        ),
        model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4"),
        tools=[add, send_email],
        plugins=[Todo()],
        workspace=Workspace.local(".", mode="trusted"),
    )
    # Default policy: cheap moves first (archive/clear old tool results),
    # an incremental LLM summary as the last resort, all decisions sticky so
    # the prompt prefix stays cache-friendly. Omit context_window to ask the
    # provider for the active model's window and fall back to the reactive
    # overflow path when the window is unknown.
    policy = Compaction(context_window=200_000)
    serve(
        agent,
        host="127.0.0.1",
        port=8000,
        context_policy=policy,
    )


if __name__ == "__main__":
    main()
