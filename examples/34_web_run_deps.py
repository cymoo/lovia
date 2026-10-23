"""Give a served agent's tools the user's identity and what they picked on the page.

Run::

    pip install -e .[web]
    python examples/34_web_run_deps.py
    # open http://127.0.0.1:8000, pick a knowledge base by the message box,
    # and ask "how do refunds work?"

Two inputs reach every run's ``RunContext.deps`` through
``create_app(run_deps=...)``:

- **What the user picked** — the knowledge base in the composer's dropdown.
  ``web_run_deps/extension.js`` reports it with
  ``lovia.provideContext("kb", ...)``; the server sees it as
  ``trigger.client["kb"]``. The browser controls this value, so it only
  selects among options the server allows, and never grants anything.
- **Who is asking** — set on ``request.state`` by the ``auth=`` dependency
  (here a demo cookie the sidebar's picker sets; a real app checks a session),
  read from ``trigger.request``. Runs nobody requested, like a schedule's
  fires, have no request.

The tools then use ``ctx.deps``: ``search`` looks in the picked knowledge base,
``whoami`` answers from the identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from fastapi import HTTPException, Request

from lovia import Agent, RunContext, enable_logging, model_from_env, tool
from lovia.web import SURFACE_NOTE, ChatUI, RunTrigger, create_app, serve

load_dotenv()
MODEL = model_from_env()  # LOVIA_MODEL etc.; raises with a hint if unset

KNOWLEDGE_BASES = {
    "docs": {
        "refunds": "Refunds go back to the original card within 5 business days.",
        "api keys": "Rotate a key by creating a new one, then revoking the old.",
    },
    "billing": {
        "refunds": "Billing issues refunds from Settings → Billing → Invoices.",
        "invoices": "Invoices are emailed on the 1st and listed under Billing.",
    },
}


@dataclass(frozen=True)
class Deps:
    user: str
    kb: str


async def auth(request: Request) -> None:
    """Demo identity: whoever the sidebar's picker says. A real app verifies
    its session cookie here and raises 401 when there is none."""
    request.state.user = request.cookies.get("acme_user", "guest")


def run_deps(trigger: RunTrigger) -> Deps:
    # Identity comes from the request — never from trigger.client.
    user = trigger.request.state.user if trigger.request else "scheduler"
    kb = str(trigger.client.get("kb", "docs"))
    if kb not in KNOWLEDGE_BASES:
        raise HTTPException(400, f"unknown knowledge base {kb!r}")
    return Deps(user=user, kb=kb)


@tool
def search(ctx: RunContext[Deps], query: str) -> str:
    """Search the knowledge base the user picked.

    Args:
        query: What to look for.
    """
    entries = KNOWLEDGE_BASES[ctx.deps.kb]
    words = query.lower().split()
    hits = [text for topic, text in entries.items() if any(w in topic for w in words)]
    found = "\n".join(hits) or "Nothing on that."
    return f"[{ctx.deps.kb}] {found}"


@tool
def whoami(ctx: RunContext[Deps]) -> str:
    """Who the user is, and which knowledge base they are asking."""
    return f"user={ctx.deps.user} kb={ctx.deps.kb}"


UI = ChatUI(
    assets=Path(__file__).parent / "web_run_deps",
    scripts=["extension.js"],
    styles=["extension.css"],
    empty_title="Ask the knowledge base",
    empty_description="Pick one by the message box; answers come from it alone.",
    empty_examples=["How do refunds work?", "Who am I signed in as?"],
    messages={
        "en": {"acme.kb": "Knowledge base", "acme.user": "Signed in as"},
        "zh": {"acme.kb": "知识库", "acme.user": "当前用户"},
    },
)


def main() -> None:
    enable_logging()
    agent = Agent(
        name="kb",
        instructions=(
            "Answer only from the knowledge base search returns; say which "
            "knowledge base you used. Use whoami when asked who the user is."
            "\n\n" + SURFACE_NOTE
        ),
        model=MODEL,
        tools=[search, whoami],
    )
    app = create_app(agent, title="Acme KB", ui=UI, auth=auth, run_deps=run_deps)
    serve(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
