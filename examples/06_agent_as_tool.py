"""Use one agent as a tool inside another.

Unlike ``handoffs`` (which transfers control), ``as_tool`` runs the sub-agent
as a self-contained call and returns its final output as the tool result.
"""

from __future__ import annotations

import asyncio
import os

from lovia import Agent, Runner

from dotenv import load_dotenv

load_dotenv()

translator = Agent(
    name="Translator",
    instructions="Translate the user's text to French. Reply with the translation only.",
    model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini"),
)

writer = Agent(
    name="Writer",
    instructions=(
        "Draft a short tweet in English, then use the translate tool to render it in French. "
        "Return both versions."
    ),
    model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini"),
    tools=[
        translator.as_tool(
            name="translate_to_french", description="Translate to French."
        )
    ],
)


async def main() -> None:
    result = await Runner.run(writer, "Announce our new dark mode.")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
