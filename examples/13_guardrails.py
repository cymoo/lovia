"""Input + output guardrails.

A guardrail is just an async (or sync) callable. Returning a reason string
(or ``True``) blocks the run with :class:`GuardrailTripped`; returning
``None``/``False`` allows it.

Run::

    python examples/13_guardrails.py
"""

from __future__ import annotations

import asyncio
from typing import Any

from dotenv import load_dotenv

from lovia import Agent, GuardrailTripped, Runner, model_from_env

load_dotenv()

MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


async def block_pii(messages: list[Any], ctx: Any) -> str | None:
    """Refuse to process anything that smells like an SSN."""
    for m in messages:
        text = (m.text or "") if hasattr(m, "text") else ""
        if "ssn" in text.lower():
            return "input mentions an SSN"
    return None


async def require_citation(output: Any, ctx: Any) -> str | None:
    """Force the model to cite a source for factual claims."""
    if isinstance(output, str) and "[source]" not in output:
        return "answer must include a [source] tag"
    return None


async def main() -> None:
    agent = Agent(
        name="careful",
        instructions=(
            "Answer questions concisely. Always include a '[source]' tag at "
            "the end of every factual answer."
        ),
        model=MODEL,
        input_guardrails=[block_pii],
        output_guardrails=[require_citation],
    )

    # 1. The input guardrail blocks this immediately.
    try:
        await Runner.run(agent, "My SSN is 123-45-6789, please remember it.")
    except GuardrailTripped as exc:
        print("blocked input:", exc)

    # 2. A normal run that passes both guardrails.
    result = await Runner.run(agent, "What is the speed of light? Cite a source.")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
