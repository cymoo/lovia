"""``think`` — a no-op scratchpad tool.

Borrowed from the Claude Code playbook: gives the model a place to write
free-form reasoning that lands in the transcript as a tool call without any
external side effect. Useful as an alternative / complement to
reasoning-token providers.

Usage::

    from lovia.builtins.think import think
    agent = Agent(name="x", tools=[think])
"""

from __future__ import annotations

from typing import Annotated

from ..tools import tool


@tool
def think(
    thought: Annotated[str, "Free-form reasoning to commit to the transcript."],
) -> str:
    """Record a reasoning step. Returns the same text so the model can react."""
    return thought
