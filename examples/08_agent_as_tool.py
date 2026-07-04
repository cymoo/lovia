"""Use one agent as a tool inside another.

Unlike ``handoffs`` (which transfers control), ``as_tool`` runs the sub-agent
as a self-contained call and returns its final output as the tool result.
The child runs its own sub-loop and does not see the parent's history.

Run::

    python examples/08_agent_as_tool.py
"""

from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from lovia import Agent, Runner, model_from_env

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

translator = Agent(
    name="Translator",
    instructions="Translate the user's text to French. Reply with the translation only.",
    model=MODEL,
)

writer = Agent(
    name="Writer",
    instructions=(
        "Draft a short tweet in English, then use the translate tool to render it in French. "
        "Return both versions."
    ),
    model=MODEL,
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
