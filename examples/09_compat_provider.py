"""Talk to any OpenAI-compatible endpoint (DeepSeek, Ollama, vLLM, ...).

Just construct ``OpenAIChatProvider`` with the ``base_url`` and ``api_key``
of the target service. Everything else (tools, sessions, handoffs, streaming)
works the same.
"""

from __future__ import annotations

import asyncio
import os

from lovia import Agent, OpenAIChatProvider, Runner

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    provider = OpenAIChatProvider(
        model="deepseek-chat",
        api_key=os.environ.get("DEEPSEEK_API_KEY") or os.environ["OPENAI_API_KEY"],
        base_url="https://api.deepseek.com/v1",
    )
    agent = Agent(
        name="DS",
        instructions="Answer in one sentence.",
        model=provider,
    )
    result = await Runner.run(agent, "What's a monad, intuitively?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
