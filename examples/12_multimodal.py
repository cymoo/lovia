"""Multimodal input: send an image alongside text.

Both OpenAI and Anthropic adapters translate :class:`ImagePart` to the
vendor's native image format. Images may be given as a URL or as base64
data + media type.
"""

from __future__ import annotations

import asyncio
import os

from dotenv import load_dotenv

from lovia import Agent, ImagePart, Runner, TextPart
from lovia.messages import user

load_dotenv()

MODEL = os.environ.get("LOVIA_MODEL")
if not MODEL:
    raise SystemExit(
        'Set LOVIA_MODEL first (env or .env), e.g. "openai:gpt-5.4" '
        'or "anthropic:claude-4-8-opus"'
    )


async def main() -> None:
    agent = Agent(
        name="VisionBot",
        instructions="Describe what you see, briefly.",
        model=MODEL,
    )

    msg = user(
        [
            TextPart(text="What's in this picture? One sentence."),
            ImagePart(
                url="https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg"
            ),
        ]
    )
    result = await Runner.run(agent, [msg])
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
