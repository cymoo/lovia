"""DX shortcuts: ``Agent.run_sync``, ``output_type`` override, and richer ``@tool`` metadata.

Run::

    OPENAI_API_KEY=sk-... python examples/21_dx.py
"""

from __future__ import annotations
import os

from typing import Annotated

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from lovia import Agent, tool

load_dotenv()
MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4")


class Summary(BaseModel):
    title: str
    bullets: list[str]


@tool(strict=True)
def shout(
    text: Annotated[str, Field(description="The text to amplify", max_length=200)],
) -> str:
    """Return ``text`` in upper case with three trailing exclamation marks."""
    return text.upper() + "!!!"


def main() -> None:
    agent = Agent(
        name="Summariser",
        instructions="You answer concisely.",
        model=MODEL,
        tools=[shout],
    )

    # 1. Synchronous one-liner — no asyncio.run() needed.
    text_result = agent.run_sync("Shout 'hi there'.")
    print("text:", text_result.output)

    # 2. Override the structured output type per call (no Agent clone required).
    summary_result = agent.run_sync(
        "Summarise lovia in three bullet points.",
        output_type=Summary,
    )
    print("\nstructured:", summary_result.output)


if __name__ == "__main__":
    main()
