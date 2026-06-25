"""Long conversations that survive the model's context window.

This example shows the default ``Compaction`` doing its job:

1. We seed an ``InMemorySession`` with a long fake transcript that would
   normally blow past a small model's context window.
2. A small ``context_window`` + a low ``compact_at`` force the pipeline to
   compact on the next turn: old tool results are cleared first (free), and
   the older prefix is folded into a running LLM summary only as a last
   resort.
3. Compaction is **view-only and sticky**: it shapes only what is sent to the
   model, while decisions are remembered per run so the prompt prefix stays
   byte-stable across turns (prompt-cache friendly). The ``Session`` is never
   modified — the full history remains the source of truth.
4. A hook listens for ``ContextCompacted`` and feeds the summary into a
   long-term ``Memory`` — the layers stay nicely orthogonal.
5. The compacting policy automatically provides ``recall_tool_result`` so the
   agent can pull back a tool output that compaction dropped from the view,
   without re-running the tool — no manual tool wiring needed.

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
    Compaction,
    RunContext,
    Runner,
    events,
)
from lovia.transcript import AssistantTextEntry, InputEntry
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
    async def _record(ev: events.ContextCompacted, ctx: RunContext) -> None:
        print(
            f"[compacted] reason={ev.reason} "
            f"tokens={ev.metadata.get('tokens_before')}→{ev.metadata.get('tokens_after')}"
        )
        if ev.summary:
            await long_term.add(ev.summary, metadata={"session_id": ev.session_id})

    agent = Agent(
        name="companion",
        instructions="You are a helpful, concise companion.",
        model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4"),
        # recall_tool_result is provided automatically by the Compaction policy.
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

    # Tight budget so this demo definitely compacts on the first real turn.
    # In production you'd set context_window to the model's actual context
    # window (or omit it and let provider.context_window decide).
    policy = Compaction(
        context_window=2_000,
        reserve_output_tokens=500,
        compact_at=0.5,
        compact_to=0.3,
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

    # The Session still holds the full, untouched history — compaction only
    # shaped the per-call view, never what was stored.
    persisted = await session.load("u-mei")
    print(f"Entries stored in the session (full history): {len(persisted)}")
    print(f"Long-term memory records (summaries): {len(long_term.records)}")


if __name__ == "__main__":
    asyncio.run(main())
