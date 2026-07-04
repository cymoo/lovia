"""Multi-turn conversation persisted with ``SQLiteSession``.

Each ``Runner.run`` call loads the prior transcript for ``session_id`` and
appends the new turns. The same ``session_id`` resumes the conversation —
even across processes; a different id is a clean slate.

Run::

    python examples/05_sessions.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from lovia import Agent, Runner, SQLiteSession

load_dotenv()
MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.5" '
        'or "anthropic:claude-4-8-opus"'
    )


async def main() -> None:
    Path("tmp").mkdir(exist_ok=True)
    session = SQLiteSession(Path("tmp/sessions.db"))
    agent = Agent(
        name="Companion",
        instructions="You remember the user across turns. Answer briefly.",
        model=MODEL,
    )

    r1 = await Runner.run(agent, "Hi, I'm Mei.", session=session, session_id="user-mei")
    print("A:", r1.output)

    # Same session_id -> the model sees the earlier turns.
    r2 = await Runner.run(
        agent, "What's my name?", session=session, session_id="user-mei"
    )
    print("A:", r2.output)

    # A different session_id shares nothing with user-mei's conversation.
    r3 = await Runner.run(
        agent, "What's my name?", session=session, session_id="user-tom"
    )
    print("A (other session):", r3.output)


if __name__ == "__main__":
    asyncio.run(main())
