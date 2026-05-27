"""Serve an agent over HTTP with a built-in chat UI.

Run::

    pip install -e .[web]
    python examples/16_web_serve.py
    # open http://127.0.0.1:8000

Demonstrates the optional ``lovia.web`` layer: REST endpoints, SSE streaming,
human-in-the-loop approval, and the bundled refined-minimal chat page.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from lovia import Agent, tool
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
    agent = Agent(
        name="lovia",
        instructions=(
            "You are a friendly assistant. Use tools when helpful. "
            "Keep replies short and conversational."
        ),
        model=os.getenv("OPENAI_DEFAULT_MODEL", "deepseek-chat"),
        tools=[add, send_email],
    )
    serve(agent, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
