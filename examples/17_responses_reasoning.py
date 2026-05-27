"""Use the OpenAI Responses API with native reasoning items.

The Responses provider (``openai-responses:<model>``) is the canonical
adapter for o-series models. Two things are different from the regular
Chat Completions adapter:

* **Reasoning is preserved.** o-series models emit an opaque encrypted
  reasoning blob (``ReasoningItem``) that *must* be replayed back on
  subsequent turns of the same conversation. Sessions store it
  automatically.
* **Server-side tools.** Tools like ``web_search``, ``file_search`` and
  ``code_interpreter`` execute on OpenAI's side; you opt in by adding
  the tool dict to ``model_settings.extra["tools"]`` (per OpenAI's
  Responses tool format).

Requires ``OPENAI_API_KEY`` in the environment. Pick any o-series or
GPT-5 model (e.g. ``o4-mini``, ``gpt-5``).
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, Runner
from lovia.providers.base import ModelSettings
from lovia.stores import InMemorySession

load_dotenv()


async def main() -> None:
    agent = Agent(
        name="reasoner",
        instructions="Think carefully, then answer concisely.",
        # The ``openai-responses:`` prefix routes through the Responses API.
        model=f"openai-responses:{os.getenv('OPENAI_RESPONSES_MODEL', 'o4-mini')}",
        # ``reasoning.effort`` is a Responses-specific knob — pass it via
        # ``settings.extra`` and it rides straight through to the request.
        settings=ModelSettings(extra={"reasoning": {"effort": "medium"}}),
    )

    # A session keeps the reasoning items around so the second turn can
    # replay them — required for coherent o-series multi-turn flows.
    session = InMemorySession()

    r1 = await Runner.run(
        agent,
        "If a train leaves at 9:42 and arrives at 11:17, how long is the trip?",
        session=session,
        session_id="train-chat",
    )
    print("first:", r1.output)

    r2 = await Runner.run(
        agent,
        "Now express that as minutes.",
        session=session,
        session_id="train-chat",
    )
    print("second:", r2.output)

    # Inspect what got persisted: notice the ReasoningItem entries.
    items = await session.load("train-chat")
    print(f"persisted {len(items)} items; types:")
    for it in items:
        print("  -", it.type)


if __name__ == "__main__":
    asyncio.run(main())
