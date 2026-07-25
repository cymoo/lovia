"""LLM-suggested follow-up questions for the chat UI.

After a turn completes, the UI can offer a few questions the user might want to
ask next, rendered as clickable chips. They are produced by a *separate* tiny
agent run — the same "separate ad-hoc model for a sub-task" pattern as
:mod:`lovia.web.titles` — so the main transcript never sees the request and a
slow suggestion call never delays the answer.

The whole feature is one knob on :func:`~lovia.web.create_app`::

    create_app(agent, followups=False)         # off (the default)
    create_app(agent, followups=True)          # the built-in suggester below
    create_app(agent, followups=my_async_fn)   # your own

A custom suggester is any ``async def f(request: FollowupRequest) ->
Sequence[str]`` — back it with a curated FAQ, a vector store, or this module's
:func:`generate_followups` under a different prompt::

    from lovia.web.followups import generate_followups

    async def only_pricing(req):
        return await generate_followups(
            req, model="<model>", instructions="Ask only about pricing."
        )

Failures are logged and swallowed: no suggestions is the neutral outcome.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..agent import Agent
from ..messages import Message
from ..providers import Provider
from ..runner import Runner

__all__ = [
    "FOLLOWUP_INSTRUCTIONS",
    "FollowupFn",
    "FollowupRequest",
    "generate_followups",
]

log = logging.getLogger(__name__)

# How much of the last exchange the suggester sees. A reply can be arbitrarily
# long (a whole file listing, a big table); the tail of it is rarely what a
# follow-up hangs off, so cut hard and keep the call small.
_MAX_QUESTION = 600
_MAX_REPLY = 1200
# Longest a single suggestion may be. They render as one-line chips — anything
# past this is a paragraph the model smuggled in, not a question.
_MAX_SUGGESTION = 120

FOLLOWUP_INSTRUCTIONS = (
    "You propose what the USER might want to ask next, given the exchange that "
    "just happened.\n"
    "Rules:\n"
    "- Write in the user's own voice — 'How do I …', never 'Would you like me to …'.\n"
    "- Use the language the conversation is in.\n"
    "- Keep each under 12 words; they render as one-line buttons.\n"
    "- Each must open new ground. Never rephrase the question just answered, "
    "and never ask for something the reply already gave.\n"
    "- Prefer concrete next steps the assistant can actually act on.\n"
    "Reply with one question per line, at most 3 lines, and nothing else — no "
    "numbering, no bullets, no preamble.\n"
    "If the exchange reached a natural end — a greeting, a thank-you, or a "
    "completed action with no meaningful next step — reply with exactly: NONE"
)
"""Prompt for the built-in suggester.

The ``NONE`` escape hatch carries most of the feature's quality: forcing three
questions after "thanks, that worked" is worse than showing none at all.
"""


@dataclass(frozen=True)
class FollowupRequest:
    """What a suggester is given: one session's recent conversation.

    ``messages`` is the chat-shaped view of the transcript (see
    :func:`lovia.transcript.entries_to_messages`), oldest first — the exchange
    to react to is at the end. ``agent`` is the registry key the session is
    served under, so a custom suggester can branch per agent.
    """

    session_id: str
    agent: str
    messages: list[Message] = field(default_factory=list)


FollowupFn = Callable[[FollowupRequest], Awaitable[Sequence[str]]]
"""A suggester: takes the conversation, returns questions (possibly none)."""


def _truncate(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _last_exchange(messages: Sequence[Message]) -> tuple[str, str]:
    """The final user question and the assistant reply that answered it.

    Scans backwards so intervening tool traffic — which carries no follow-up
    signal of its own — is skipped without special-casing it.
    """
    reply = ""
    reply_at = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant" and messages[i].text.strip():
            reply, reply_at = messages[i].text, i
            break
    question = ""
    for i in range(reply_at - 1, -1, -1):
        if messages[i].role == "user" and messages[i].text.strip():
            question = messages[i].text
            break
    return question, reply


# Leading list decoration a model adds despite being told not to.
_BULLET = re.compile(r"^\s*(?:[-*•–—]|\(?\d+[.):])\s*")


def parse_followups(raw: str, *, limit: int = 3) -> list[str]:
    """Parse the suggester's reply into clean, de-duplicated questions.

    Line-based rather than JSON: every provider can produce one-per-line
    reliably, and a malformed line degrades to one dropped suggestion instead
    of losing the whole batch.
    """
    if raw.strip().upper() == "NONE":
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        if len(out) >= limit:
            break
        cleaned = _BULLET.sub("", line).strip().strip("\"'`").strip()
        if not cleaned or cleaned.upper() == "NONE":
            continue
        cleaned = cleaned[:_MAX_SUGGESTION]
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


async def generate_followups(
    request: FollowupRequest,
    *,
    model: str | Provider,
    instructions: str = FOLLOWUP_INSTRUCTIONS,
    limit: int = 3,
) -> list[str]:
    """Suggest what the user might ask next; ``[]`` when nothing fits.

    ``model`` is anything :class:`Agent` accepts — a ``"provider:model"`` string
    or a :class:`~lovia.providers.Provider`. Point it at a small, cheap model:
    the call is one short prompt and a handful of output tokens.

    ``limit`` is a hard cap applied while parsing; the default ``instructions``
    separately ask the model for at most 3. Raise both together if you want
    more.
    """
    question, reply = _last_exchange(request.messages)
    if not reply.strip():
        # Nothing was answered (an empty or tool-only turn) — no ground to
        # suggest from, and no reason to spend a call finding that out.
        return []

    prompt = (
        f"USER: {_truncate(question, _MAX_QUESTION)}\n\n"
        f"ASSISTANT: {_truncate(reply, _MAX_REPLY)}\n\n"
        f"Follow-up questions:"
    )
    suggester: Agent[Any] = Agent(
        name="followups", instructions=instructions, model=model
    )
    try:
        result = await Runner.run(suggester, prompt)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("follow-up generation failed: %s", exc)
        return []

    raw = result.output if isinstance(result.output, str) else str(result.output)
    return parse_followups(raw, limit=limit)
