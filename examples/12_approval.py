"""Human-in-the-loop tool approval.

Every tool carries a ``permission``: ``"allow"`` runs, ``"ask"`` pauses the
runner with an ``ApprovalRequired`` event, ``"deny"`` refuses the call and
tells the model why. A predicate decides per call, so only the risky subset
asks — and a ``Verdict`` carries the *reason* the human (or the model) sees.
Resolve with ``ev.approve()`` / ``ev.reject(reason)`` from the streaming
loop; if nobody resolves the request, the call is denied — runs never hang.

For a fully programmatic policy (no human at all), set
``Agent(approval_handler=...)`` returning ``"allow"`` / ``"deny"`` / ``"ask"``.

Run::

    python examples/12_approval.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from dotenv import load_dotenv

from lovia import Agent, RunContext, Runner, Verdict, events, tool, model_from_env

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


def mail_policy(args: dict[str, Any], ctx: RunContext[Any]) -> str | Verdict:
    """Internal mail just goes; external mail needs a human sign-off."""
    to = str(args.get("to", ""))
    if to.endswith("@example.com"):
        return "allow"
    return Verdict("ask", f"Sends mail outside the company, to {to}.")


@tool(permission=mail_policy)
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
            # without asking. ``ev.reason`` is the verdict's explanation.
            print(f"\n[approval needed] {ev.reason}")
            print(f"  {ev.call.name}({ev.call.arguments})")
            answer = input("approve? [y/N/reason] ").strip()
            if answer.lower() == "y":
                ev.approve()
            else:
                # Anything else is a denial; free text becomes the reason
                # the model sees, so it can change course instead of retrying.
                ev.reject("" if answer.lower() in ("", "n") else answer)

    result = await handle.result()
    print(f"\n\n[done] {result.output}")


if __name__ == "__main__":
    asyncio.run(main())
