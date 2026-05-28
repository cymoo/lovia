"""Prompt Chaining workflow.

Decomposes a task into a fixed sequence of LLM calls where each step processes
the output of the previous one. A programmatic gate between steps ensures the
process stays on track before committing to the next stage.

Pipeline:
  1. Generate a structured outline for a blog post.
  2. Gate: verify the outline has at least 3 sections (skip expansion otherwise).
  3. Expand the outline into a full draft.
  4. Translate the draft into Chinese.

Reference:
  https://www.anthropic.com/engineering/building-effective-agents#prompt-chaining

Run::

    python examples/workflows/01_prompt_chaining.py
"""

from __future__ import annotations

import asyncio
import os

from pydantic import BaseModel

from lovia import Agent, Runner
from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


# ---------------------------------------------------------------------------
# Output schemas
# ---------------------------------------------------------------------------

class Section(BaseModel):
    title: str
    key_points: list[str]


class Outline(BaseModel):
    topic: str
    sections: list[Section]


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

outliner = Agent(
    name="Outliner",
    instructions=(
        "You are a technical blog strategist. "
        "Given a topic, produce a structured outline with clear sections and key points."
    ),
    model=MODEL,
    output_type=Outline,
)

expander = Agent(
    name="Expander",
    instructions=(
        "You are a skilled technical writer. "
        "Given a structured outline (as JSON), write a concise but complete blog post draft. "
        "Use markdown headings for each section."
    ),
    model=MODEL,
)

translator = Agent(
    name="Translator",
    instructions=(
        "You are a professional translator. "
        "Translate the provided English text into Chinese (Simplified). "
        "Preserve all markdown formatting."
    ),
    model=MODEL,
)


# ---------------------------------------------------------------------------
# Gate: programmatic check between steps
# ---------------------------------------------------------------------------

def check_outline(outline: Outline) -> bool:
    """Return True only if the outline has enough substance to expand."""
    return len(outline.sections) >= 3


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(topic: str) -> str:
    print(f"[Step 1] Generating outline for: {topic!r}")
    outline_result = await Runner.run(outliner, topic)
    outline: Outline = outline_result.output

    print(f"         → {len(outline.sections)} sections produced")

    # --- Gate ---
    if not check_outline(outline):
        print("[Gate]   Outline too thin — stopping pipeline.")
        return f"Outline for '{topic}' did not pass the gate (fewer than 3 sections)."

    print("[Gate]   Outline passed ✓")

    print("[Step 2] Expanding outline into a draft …")
    draft_result = await Runner.run(
        expander,
        f"Outline (JSON):\n{outline.model_dump_json(indent=2)}\n\nWrite the full blog post.",
    )
    draft: str = draft_result.output

    print("[Step 3] Translating draft to Chinese …")
    translated_result = await Runner.run(translator, draft)

    print("\n--- Final Output (Chinese) ---\n")
    return translated_result.output


async def main() -> None:
    result = await run_pipeline("Why every developer should learn async programming")
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
