"""Capstone: a small but complete terminal support bot.

Everything from earlier examples in one ~100-line app:

* streaming output with tool-call progress,
* a ``needs_approval`` refund gate resolved from the keyboard,
* ``SQLiteSession`` persistence — restart the script and it remembers,
* default ``Compaction`` so a long-running chat never overflows the
  model's context window.

Run::

    python examples/30_support_bot.py           # interactive chat
    python examples/30_support_bot.py --demo    # scripted two-turn pass

Type ``/new`` for a fresh conversation, ``/quit`` to exit.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from lovia import Agent, Compaction, Runner, Session, SQLiteSession, events, tool

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )

ORDERS = {
    "A-1001": {"item": "Mechanical keyboard", "status": "delivered", "price": 89.00},
    "A-1002": {"item": "Laptop stand", "status": "in transit", "price": 39.00},
}


@tool
async def lookup_order(order_id: str) -> str:
    """Look up an order by id (e.g. 'A-1001')."""
    order = ORDERS.get(order_id.upper())
    if order is None:
        return f"No order {order_id!r} found."
    return f"{order_id}: {order['item']} — {order['status']}, ${order['price']:.2f}"


@tool(needs_approval=True)
async def issue_refund(order_id: str, reason: str) -> str:
    """Refund an order in full. Requires human approval."""
    order = ORDERS.get(order_id.upper())
    if order is None:
        return f"No order {order_id!r} found."
    return f"Refunded ${order['price']:.2f} for {order_id} ({reason})."


agent = Agent(
    name="support",
    instructions=(
        "You are the support agent of a small electronics shop. Look orders "
        "up before answering; never invent order data. Refunds go through "
        "issue_refund. Keep replies short and warm."
    ),
    model=MODEL,
    tools=[lookup_order, issue_refund],
)

# Created once and reused: compaction decisions are sticky per run, and the
# policy is stateless across runs, so sharing one instance is the norm.
POLICY = Compaction()


async def one_turn(
    session: Session, session_id: str, text: str, *, auto_approve: bool = False
) -> None:
    handle = Runner.stream(
        agent, text, session=session, session_id=session_id, context_policy=POLICY
    )
    async for ev in handle:
        if isinstance(ev, events.TextDelta):
            print(ev.delta, end="", flush=True)
        elif isinstance(ev, events.ToolCallStarted):
            print(f"\n  [tool] {ev.call.name}({ev.call.arguments})")
        elif isinstance(ev, events.ApprovalRequired):
            print(f"\n  [approval] {ev.call.name}({ev.call.arguments})")
            if auto_approve:
                print("  [approval] auto-approved (--demo)")
                ev.approve()
            else:
                answer = input("  approve? [y/N] ").strip().lower()
                ev.approve() if answer == "y" else ev.reject()
    await handle.result()
    print()


async def interactive() -> None:
    Path("tmp").mkdir(exist_ok=True)
    session = SQLiteSession(Path("tmp/support_bot.db"))
    session_id = "walk-in"  # fixed id: restarting the script resumes the chat
    print("Support bot ready (orders A-1001, A-1002). /new starts over, /quit exits.")
    while True:
        try:
            text = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/new":
            session_id = f"chat-{uuid.uuid4().hex[:8]}"
            print("(started a fresh conversation)")
            continue
        print("bot> ", end="", flush=True)
        await one_turn(session, session_id, text)


async def demo() -> None:
    Path("tmp").mkdir(exist_ok=True)
    session = SQLiteSession(Path("tmp/support_bot.db"))
    session_id = f"demo-{uuid.uuid4().hex[:8]}"
    for text in (
        "Hi! Where is my order A-1002?",
        "The keyboard from A-1001 arrived broken. I want my money back.",
    ):
        print(f"\nyou> {text}\nbot> ", end="", flush=True)
        await one_turn(session, session_id, text, auto_approve=True)


if __name__ == "__main__":
    asyncio.run(demo() if "--demo" in sys.argv else interactive())
