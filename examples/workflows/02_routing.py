"""Routing workflow.

Classifies an incoming customer-support message and routes it to the most
appropriate specialist agent. Each specialist has a focused system prompt
tuned for its domain, avoiding the performance trade-offs of a single
catch-all prompt.

Route map:
  • billing    → refunds, invoices, subscription questions
  • technical  → bugs, crashes, error messages
  • general    → everything else

Reference:
  https://www.anthropic.com/engineering/building-effective-agents#routing

Run::

    python examples/workflows/02_routing.py
"""

from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel

from lovia import Agent, Runner, model_from_env
from dotenv import load_dotenv

load_dotenv()

MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class RouteDecision(BaseModel):
    category: Literal["billing", "technical", "general"]
    reason: str


router = Agent(
    name="Router",
    instructions=(
        "Classify the customer message into exactly one category:\n"
        "  • billing   – refunds, invoices, subscription, payment\n"
        "  • technical – bugs, crashes, errors, product not working\n"
        "  • general   – anything else (how-to questions, feature requests, etc.)\n\n"
        "Return a JSON object with 'category' and 'reason'."
    ),
    model=MODEL,
    output_type=RouteDecision,
)


# ---------------------------------------------------------------------------
# Specialist agents
# ---------------------------------------------------------------------------

billing_agent = Agent(
    name="BillingSpecialist",
    instructions=(
        "You are a billing specialist. Help customers with invoices, refunds, "
        "and subscription management. Be empathetic and solution-oriented. "
        "Always confirm the resolution before closing."
    ),
    model=MODEL,
)

technical_agent = Agent(
    name="TechnicalSupport",
    instructions=(
        "You are a senior technical support engineer. Diagnose product issues "
        "systematically: ask for reproduction steps, environment details, and "
        "relevant error messages. Provide clear, actionable troubleshooting steps."
    ),
    model=MODEL,
)

general_agent = Agent(
    name="GeneralSupport",
    instructions=(
        "You are a friendly customer-success agent. Answer general questions, "
        "explain product features, and guide customers to the right resources. "
        "Keep responses concise and helpful."
    ),
    model=MODEL,
)

SPECIALIST_MAP = {
    "billing": billing_agent,
    "technical": technical_agent,
    "general": general_agent,
}


# ---------------------------------------------------------------------------
# Router orchestration
# ---------------------------------------------------------------------------


async def handle_message(message: str) -> None:
    print(f"Customer: {message}\n")

    decision_result = await Runner.run(router, message)
    decision: RouteDecision = decision_result.output

    print(f"[Router] category={decision.category!r}  reason={decision.reason!r}")

    specialist = SPECIALIST_MAP[decision.category]
    print(f"[Router] Routing to → {specialist.name}\n")

    response = await Runner.run(specialist, message)
    print(f"{specialist.name}: {response.output}\n")
    print("-" * 60)


async def main() -> None:
    messages = [
        "I was charged twice for my subscription last month, please help.",
        "The app crashes every time I try to export a PDF. Here's the error: NullPointerException at line 42.",
        "Can you explain the difference between the free plan and the pro plan?",
    ]
    for msg in messages:
        await handle_message(msg)


if __name__ == "__main__":
    asyncio.run(main())
