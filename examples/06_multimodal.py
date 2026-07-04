"""Multimodal input: send an image alongside text.

Both the OpenAI and Anthropic adapters translate :class:`ImagePart` to the
vendor's native image format. Images may be given as a URL or as base64
``data`` + ``media_type``.

Run::

    python examples/06_multimodal.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, ImagePart, Runner, TextPart, user, model_from_env

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


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
