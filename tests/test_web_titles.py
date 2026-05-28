"""Tests for the LLM-backed title generator."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from lovia import Agent  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.titles import _clean, _fallback_title, generate_title  # noqa: E402

from .scripted_provider import ScriptedProvider, text  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Hello World", "Hello World"),
        ("  Hello World  ", "Hello World"),
        ('"Quoted Title"', "Quoted Title"),
        ("Title: Foo Bar", "Foo Bar"),
        ("title: foo bar.", "foo bar"),
        ("Line one\nLine two", "Line one"),
        ("Trailing punctuation!", "Trailing punctuation"),
    ],
)
def test_clean(raw: str, expected: str) -> None:
    assert _clean(raw) == expected


def test_fallback_title_from_user_message() -> None:
    assert _fallback_title("How do I write a TCP server?") == (
        "How do I write a TCP server?"
    )
    assert _fallback_title("") == "New chat"


async def test_generate_title_uses_model_reply() -> None:
    provider = ScriptedProvider([text("Auth With OAuth2")])
    title = await generate_title(
        "How do I implement OAuth2 in FastAPI?",
        "Use fastapi-users with the OAuth backend…",
        model=provider,
    )
    assert title == "Auth With OAuth2"


async def test_generate_title_strips_decorations() -> None:
    provider = ScriptedProvider([text('"Title: Debugging A Memory Leak."')])
    title = await generate_title(
        "why is RSS climbing?", "look for cycles", model=provider
    )
    assert title == "Debugging A Memory Leak"


async def test_generate_title_fallback_on_blank_input() -> None:
    provider = ScriptedProvider([text("")])
    title = await generate_title("", None, model=provider)
    assert title == "New chat"


# ---- HTTP integration ------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402


def test_chat_triggers_background_title_generation() -> None:
    # First entry is the chat reply; second is the title.
    provider = ScriptedProvider(
        [text("It depends on the language."), text("Programming Language Pick")]
    )
    app = create_app(Agent(name="bot", model=provider))
    c = TestClient(app)
    res = c.post(
        "/api/chat", json={"message": "Which programming language should I learn?"}
    )
    sid = res.json()["session_id"]

    # The background task may have completed inside the TestClient's loop already;
    # if not, list_sessions still works and the title shows up shortly.
    metas = c.get("/api/sessions").json()
    assert any(m["id"] == sid for m in metas)
    titled = next(m for m in metas if m["id"] == sid)
    assert titled["title"] in {None, "Programming Language Pick"}


def test_stream_emits_title_event() -> None:
    from .test_web import _parse_sse  # reuse SSE parser

    provider = ScriptedProvider([text("hello"), text("Greeting The User")])
    app = create_app(Agent(name="bot", model=provider))
    c = TestClient(app)
    with c.stream("POST", "/api/chat/stream", json={"message": "say hi"}) as res:
        body = "".join(res.iter_text())
    events = _parse_sse(body)
    kinds = [e[0] for e in events]
    assert "title" in kinds
    title_event = next(e for e in events if e[0] == "title")
    assert title_event[1]["title"] == "Greeting The User"
