"""Human-in-the-loop tool approval.

Tools gated with ``needs_approval`` pause the runner and emit an
``ApprovalRequired`` event; resolve it with ``ev.approve()`` / ``ev.reject()``
from the streaming loop. ``needs_approval`` also takes a predicate, so only
the risky subset of calls asks. If nobody resolves the request, the call is
denied — runs never hang on an absent decision.

For a fully programmatic policy (no human at all), set
``Agent(approval_handler=...)`` returning ``"allow"`` / ``"deny"`` / ``"ask"``.

Run::

    python examples/12_approval.py
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from dotenv import load_dotenv

from lovia import Agent, RunContext, Runner, events, tool

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


def is_external(args: dict[str, Any], ctx: RunContext[Any]) -> bool:
    """Only mail leaving the company needs a human sign-off."""
    return not str(args.get("to", "")).endswith("@example.com")


@tool(needs_approval=is_external)
async def send_email(to: str, subject: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="Help with email tasks. Always use send_email for outgoing mail.",
        model=MODEL,
        tools=[send_email],
    )

    handle = Runner.stream(
        agent,
        "Send a one-line hello to bob@example.com, "
        "then the same hello to alice@other.org.",
    )
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ApprovalRequired):
            # Only the alice@other.org call lands here; the internal one ran
            # without asking.
            print(f"\n[approval needed] {ev.call.name}({ev.call.arguments})")
            answer = input("approve? [y/N] ").strip().lower()
            ev.approve() if answer == "y" else ev.reject()

    result = await handle.result()
    print(f"\n\n[done] {result.output}")


if __name__ == "__main__":
    asyncio.run(main())
