"""Coding agent with a local workspace.

Shell commands the policy marks "ask" pause the stream for approval; the
allow rules below let read-only commands run without prompting.

Run::

    python examples/23_workspace_agent.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from lovia import Agent, Runner, events
from lovia.workspace import CommandRule, Workspace

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )
console = Console()


async def main() -> None:
    agent = Agent(
        name="coder",
        instructions="You are a careful coding agent. Inspect files before editing.",
        model=MODEL,
        workspace=Workspace.local(
            ".",
            mode="coding",
            command_rules=(
                CommandRule("git status", "allow"),
                CommandRule("git diff", "allow"),
                CommandRule("ls", "allow"),
                CommandRule("rm -rf", "deny"),
            ),
        ),
    )
    handle = Runner.stream(
        agent, "List the top-level Python files and explain their purpose.", max_turns=6
    )
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            console.print(ev.delta, end="", soft_wrap=True, markup=False)
        elif isinstance(ev, events.ToolCallStarted):
            console.print(f"\n[cyan]tool[/cyan] {ev.call.name}({ev.call.arguments})")
        elif isinstance(ev, events.ToolCallCompleted):
            status = "error" if ev.is_error else "done"
            style = "red" if ev.is_error else "green"
            console.print(f"[{style}]tool:{status}[/{style}] {ev.call.name}")
        elif isinstance(ev, events.ApprovalRequired):
            console.print(
                Panel(
                    f"{ev.call.name}({ev.call.arguments})",
                    title="Approval needed",
                    border_style="yellow",
                )
            )
            answer = console.input("approve? [y/N] ").strip().lower()
            ev.approve() if answer == "y" else ev.reject()

    result = await handle.result()
    console.print(f"\n[dim]done · turns={result.turns}[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
