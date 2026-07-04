"""Human-in-the-loop tool approval.

Tools declared with ``needs_approval=True`` pause the runner and emit an
``ApprovalRequired`` event. Resolve it by calling ``ev.approve()`` /
``ev.reject()`` from the streaming loop, or by setting
``Agent.approval_handler`` for a fully programmatic policy.

If no one resolves the request, the call is denied — runs never hang on an
absent decision.
"""

from __future__ import annotations

import os
import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, events, tool

load_dotenv()

MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.4" '
        'or "anthropic:claude-4-8-opus"'
    )


@tool(needs_approval=True)
async def send_email(to: str, subject: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


async def main() -> None:
    agent = Agent(
        name="Assistant",
        instructions="Help the user with email tasks. Always call send_email for outgoing messages.",
        model=MODEL,
        tools=[send_email],
    )

    handle = Runner.stream(
        agent, "Email alice@example.com a one-line hello from lovia."
    )
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ApprovalRequired):
            print(f"\n[approval needed] {ev.call.name}({ev.call.arguments})")
            answer = input("approve? [y/N] ").strip().lower()
            if answer == "y":
                ev.approve()
            else:
                ev.reject()

    result = await handle.result()
    print(f"\n\n[done] {result.output}")


if __name__ == "__main__":
    asyncio.run(main())
