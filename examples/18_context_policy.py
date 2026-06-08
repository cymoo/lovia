"""Long conversations that survive the model's context window.

This example shows ``CompactingContextPolicy`` doing its job:

1. We seed an ``InMemorySession`` with a long fake transcript that would
   normally blow past a small model's context window.
2. A small ``context_window_tokens`` + ``trigger_ratio=0.5`` forces the policy to
   summarize on the next turn.
3. The ``archive`` callback captures the pre-compaction transcript so we
   can audit it offline.
4. A hook listens for ``ContextCompacted`` and feeds the summary into a
   long-term ``Memory`` — the three layers stay nicely orthogonal.

Run::

    python examples/18_context_policy.py
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import (
    Agent,
    AgentHooks,
    AssistantTextEntry,
    InputEntry,
    Runner,
    CompactingContextPolicy,
    events,
)
from lovia.stores import InMemorySession

load_dotenv()


# A toy in-memory long-term memory so the example stays self-contained.
class _DictMemory:
    def __init__(self) -> None:
        self.records: list[str] = []

    async def add(self, content: str, *, metadata=None) -> None:
        self.records.append(content)

    async def retrieve(self, query: str, *, k: int = 5):
        return []


async def main() -> None:
    long_term = _DictMemory()

    # Hook into ContextCompacted to feed the summary into long-term memory.
    hooks = AgentHooks()

    @hooks.on(events.ContextCompacted)
    async def _record(ev: events.ContextCompacted) -> None:
        if ev.summary:
            await long_term.add(ev.summary, metadata={"session_id": ev.session_id})

    agent = Agent(
        name="companion",
        instructions="You are a helpful, concise companion.",
        model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4"),
        hooks=hooks,
    )

    # Pre-seed a session with 30 fake turns so the next call is "huge".
    session = InMemorySession()
    seeded: list = []
    for i in range(30):
        seeded.append(
            InputEntry(
                role="user",
                content=f"User trivia round {i}: tell me a fact about pandas.",
            )
        )
        seeded.append(
            AssistantTextEntry(
                content=f"Fact #{i}: pandas eat about 12kg of bamboo daily."
            )
        )
    await session.append("u-mei", seeded)

    # Write-only archive: a one-liner persists the pre-compaction transcript.
    archive_log: list = []

    async def archive(ev) -> None:
        archive_log.append(
            {
                "session_id": ev.session_id,
                "n_before": len(ev.entries_before),
                "n_after": len(ev.entries_after),
                "summary": ev.summary,
                "reactive": ev.reactive,
            }
        )

    # Tight budget so this demo definitely triggers compaction on the
    # first real turn. In production you'd set max_tokens to the model's
    # actual context window (or omit it and let provider.context_window
    # decide).
    policy = CompactingContextPolicy(
        context_window_tokens=2_000,
        trigger_ratio=0.5,  # threshold = 1000 tokens
        keep_recent_entries=4,
        archive=archive,
    )

    result = await Runner.run(
        agent,
        "Now in one sentence, who am I talking to?",
        session=session,
        session_id="u-mei",
        context_policy=policy,
    )
    print("Assistant:", result.output)
    print()
    print(f"Archive entries: {len(archive_log)}")
    for entry in archive_log:
        print(
            f"  • session={entry['session_id']!r} "
            f"before={entry['n_before']} after={entry['n_after']} "
            f"reactive={entry['reactive']}"
        )
        if entry["summary"]:
            print(f"    summary preview: {entry['summary'][:120]}...")
    print()
    print(f"Long-term memory records: {len(long_term.records)}")


if __name__ == "__main__":
    asyncio.run(main())
