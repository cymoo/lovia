"""Tests for suggested follow-up questions."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from lovia import Agent  # noqa: E402
from lovia.messages import Message  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.followups import (  # noqa: E402
    FollowupRequest,
    _last_exchange,
    generate_followups,
    parse_followups,
)

from ..scripted_provider import ScriptedProvider, text  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("A?\nB?\nC?", ["A?", "B?", "C?"]),
        ("  A?  \n\n  B?  ", ["A?", "B?"]),
        # List decoration the model adds despite being told not to.
        ("- A?\n- B?", ["A?", "B?"]),
        ("1. A?\n2. B?", ["A?", "B?"]),
        ("1) A?\n(2) B?", ["A?", "B?"]),
        ("• A?\n— B?", ["A?", "B?"]),
        ('"A?"\n`B?`', ["A?", "B?"]),
        # The escape hatch, alone or as the only surviving line.
        ("NONE", []),
        ("  none  ", []),
        ("NONE\n", []),
        # Over-generation is capped, not an error.
        ("A?\nB?\nC?\nD?\nE?", ["A?", "B?", "C?"]),
        # Case-insensitive de-duplication.
        ("A?\na?\nB?", ["A?", "B?"]),
    ],
)
def test_parse_followups(raw: str, expected: list[str]) -> None:
    assert parse_followups(raw) == expected


def test_parse_followups_respects_limit() -> None:
    assert parse_followups("A?\nB?\nC?", limit=2) == ["A?", "B?"]
    assert parse_followups("A?\nB?", limit=0) == []


def test_parse_followups_truncates_a_runaway_line() -> None:
    """A model that answers with a paragraph must not blow out the chip row."""
    out = parse_followups("x" * 500)
    assert len(out) == 1
    assert len(out[0]) == 120


def test_last_exchange_picks_the_final_pair() -> None:
    messages = [
        Message(role="user", content="first"),
        Message(role="assistant", content="one"),
        Message(role="user", content="second"),
        Message(role="assistant", content="two"),
    ]
    assert _last_exchange(messages) == ("second", "two")


def test_last_exchange_skips_tool_traffic() -> None:
    """Tool calls and results carry no follow-up signal; the pair spans them."""
    messages = [
        Message(role="user", content="what's the weather?"),
        Message(role="assistant", content=None),
        Message(role="tool", content="sunny, 21C", tool_call_id="c1"),
        Message(role="assistant", content="It's sunny and 21°C."),
    ]
    assert _last_exchange(messages) == (
        "what's the weather?",
        "It's sunny and 21°C.",
    )


def test_last_exchange_without_a_reply() -> None:
    assert _last_exchange([Message(role="user", content="hi")]) == ("hi", "")
    assert _last_exchange([]) == ("", "")


def _request(question: str, reply: str) -> FollowupRequest:
    return FollowupRequest(
        session_id="s1",
        agent="bot",
        messages=[
            Message(role="user", content=question),
            Message(role="assistant", content=reply),
        ],
    )


async def test_generate_followups_parses_the_model_reply() -> None:
    provider = ScriptedProvider(
        [text("How do I add auth?\nWhat about rate limits?\nCan I deploy it?")]
    )
    out = await generate_followups(
        _request("How do I build a FastAPI app?", "Start with a router…"),
        model=provider,
    )
    assert out == [
        "How do I add auth?",
        "What about rate limits?",
        "Can I deploy it?",
    ]


async def test_generate_followups_honours_none() -> None:
    provider = ScriptedProvider([text("NONE")])
    out = await generate_followups(
        _request("thanks, that worked!", "Glad it helped."), model=provider
    )
    assert out == []


async def test_generate_followups_skips_an_unanswered_turn() -> None:
    """No reply to hang a follow-up off — and no call spent finding that out."""
    provider = ScriptedProvider([text("unused")])
    request = FollowupRequest(
        session_id="s1", agent="bot", messages=[Message(role="user", content="hi")]
    )
    assert await generate_followups(request, model=provider) == []
    assert provider.calls == []


async def test_generate_followups_truncates_a_long_reply() -> None:
    provider = ScriptedProvider([text("A?")])
    await generate_followups(_request("q" * 5000, "r" * 9000), model=provider)
    prompt = provider.calls[0][-1].text
    # Both halves are cut, and the ellipsis marks where.
    assert "…" in prompt
    assert len(prompt) < 2200


async def test_generate_followups_swallows_a_provider_failure() -> None:
    provider = ScriptedProvider([])  # raises on the first call
    assert await generate_followups(_request("q", "r"), model=provider) == []


# ---- HTTP integration ------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402


def _app(provider: ScriptedProvider, **kw: object) -> object:
    kw.setdefault("generate_titles", False)
    return create_app(Agent(name="bot", model=provider), **kw)  # type: ignore[arg-type]


def test_followups_are_off_by_default() -> None:
    """Off unless asked for — and provably without spending a model call."""
    provider = ScriptedProvider([text("Paris.")])
    c = TestClient(_app(provider))
    sid = c.post("/api/chat", json={"message": "capital of France?"}).json()[
        "session_id"
    ]

    res = c.post(f"/api/sessions/{sid}/followups")
    assert res.status_code == 200
    assert res.json() == {"followups": []}
    assert len(provider.calls) == 1  # the chat turn only
    assert c.get("/api/info").json()["features"]["followups"] is False


def test_followups_endpoint_suggests_questions() -> None:
    provider = ScriptedProvider(
        [text("Paris."), text("What's its population?\nBest time to visit?")]
    )
    c = TestClient(_app(provider, followups=True))
    sid = c.post("/api/chat", json={"message": "capital of France?"}).json()[
        "session_id"
    ]

    body = c.post(f"/api/sessions/{sid}/followups").json()
    assert body["followups"] == ["What's its population?", "Best time to visit?"]
    assert c.get("/api/info").json()["features"]["followups"] is True


def test_followups_accept_a_custom_suggester() -> None:
    """``followups=<callable>`` replaces the built-in LLM suggester wholesale."""
    seen: list[FollowupRequest] = []

    async def canned(request: FollowupRequest) -> list[str]:
        seen.append(request)
        return ["Tell me more", "Show an example"]

    provider = ScriptedProvider([text("Paris.")])
    c = TestClient(_app(provider, followups=canned))
    sid = c.post("/api/chat", json={"message": "capital of France?"}).json()[
        "session_id"
    ]

    body = c.post(f"/api/sessions/{sid}/followups").json()
    assert body["followups"] == ["Tell me more", "Show an example"]
    # No model call beyond the chat turn — the custom suggester owns it all.
    assert len(provider.calls) == 1
    assert seen[0].session_id == sid
    assert seen[0].agent == "bot"
    assert [m.role for m in seen[0].messages][-2:] == ["user", "assistant"]
    assert c.get("/api/info").json()["features"]["followups"] is True


def test_followups_from_a_failing_suggester_degrade_to_none() -> None:
    async def boom(request: FollowupRequest) -> list[str]:
        raise RuntimeError("vector store is down")

    provider = ScriptedProvider([text("Paris.")])
    c = TestClient(_app(provider, followups=boom))
    sid = c.post("/api/chat", json={"message": "capital of France?"}).json()[
        "session_id"
    ]

    res = c.post(f"/api/sessions/{sid}/followups")
    assert res.status_code == 200
    assert res.json() == {"followups": []}


def test_followups_for_an_unknown_session_are_404() -> None:
    c = TestClient(_app(ScriptedProvider([]), followups=True))
    assert c.post("/api/sessions/nope/followups").status_code == 404


def test_followup_model_overrides_the_agent_model() -> None:
    """The suggestion call can be pointed at a cheaper model than the agent's."""
    chat = ScriptedProvider([text("Paris.")])
    cheap = ScriptedProvider([text("What's its population?")])
    c = TestClient(_app(chat, followups=True, followup_model=cheap))
    sid = c.post("/api/chat", json={"message": "capital of France?"}).json()[
        "session_id"
    ]

    body = c.post(f"/api/sessions/{sid}/followups").json()
    assert body["followups"] == ["What's its population?"]
    assert len(chat.calls) == 1  # the agent's model never saw the suggestion prompt
    assert len(cheap.calls) == 1
