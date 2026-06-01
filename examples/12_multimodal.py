"""Multimodal input: send an image alongside text.

Both OpenAI and Anthropic adapters translate :class:`ImageBlock` to the
vendor's native image format. Images may be given as a URL or as base64
data + media type.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, ImageBlock, Runner, TextBlock
from lovia.messages import user

load_dotenv()


async def main() -> None:
    agent = Agent(
        name="VisionBot",
        instructions="Describe what you see, briefly.",
        model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4"),
    )

    msg = user(
        [
            TextBlock(text="What's in this picture? One sentence."),
            ImageBlock(
                url="https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg"
            ),
        ]
    )
    result = await Runner.run(agent, [msg])
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
