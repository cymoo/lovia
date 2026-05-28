"""LLM-driven chat title generation.

After the first turn of a brand-new session, we ask the model itself to
distill the conversation into a 3-6 word title. Done as a *separate* tiny
agent run so it doesn't pollute the main transcript and so a slow title
call never blocks the user's next message.

Failures are logged and swallowed — a missing title is harmless and the
UI falls back to "Untitled chat".
"""

from __future__ import annotations

import logging
from typing import Any

from ..agent import Agent
from ..runner import Runner

__all__ = ["generate_title", "TITLE_INSTRUCTIONS"]

log = logging.getLogger(__name__)


TITLE_INSTRUCTIONS = (
    "You write a 3-6 word title that summarises a chat. "
    "Reply with ONLY the title — no quotes, no trailing punctuation, "
    "no 'Title:' prefix. Use the chat's own language. "
    "Capitalise like a headline."
)


def _truncate(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


async def generate_title(
    user_message: str,
    assistant_reply: Any,
    *,
    model: Any,
) -> str:
    """Return a short, human-friendly title for the chat.

    ``model`` may be a ``"provider:model"`` string or a ``Provider``
    instance — anything :class:`Agent` accepts. The reply is normalised
    (whitespace collapsed, trailing punctuation trimmed) before return.
    """
    if not user_message.strip():
        return "New chat"

    reply_text = ""
    if isinstance(assistant_reply, str):
        reply_text = assistant_reply
    elif assistant_reply is not None:
        reply_text = str(assistant_reply)

    prompt = (
        f"USER: {_truncate(user_message, 600)}\n\n"
        f"ASSISTANT: {_truncate(reply_text, 600)}\n\n"
        f"Title:"
    )

    titler: Agent[Any] = Agent(
        name="titler",
        instructions=TITLE_INSTRUCTIONS,
        model=model,
    )
    try:
        result = await Runner.run(titler, prompt)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("title generation failed: %s", exc)
        return _fallback_title(user_message)

    raw = result.output if isinstance(result.output, str) else str(result.output)
    return _clean(raw) or _fallback_title(user_message)


def _clean(s: str) -> str:
    s = " ".join(s.strip().splitlines()[0:1]).strip()  # first line only
    s = s.strip("\"'`")
    for prefix in ("Title:", "title:", "TITLE:"):
        if s.startswith(prefix):
            s = s[len(prefix) :].strip()
    s = s.rstrip(".!?,;:")
    return s[:120]


def _fallback_title(user_message: str) -> str:
    head = user_message.strip().split("\n", 1)[0]
    return _truncate(head, 60) or "New chat"
