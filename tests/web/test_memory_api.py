"""Tests for the /api/memory endpoints (the sidebar Memory editor).

The routes are a thin shell over ``Memory.notes_body`` / ``Memory.replace_notes``
(policy is tested with the plugin); here we pin discovery (feature flag +
per-agent ``memory`` flag), the GET/PUT round-trip with its meter fields, and
the 404 shape for agents without the plugin.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.plugins.memory import Memory  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "mem", index=None, auto_curate=False)


@pytest.fixture()
def client(mem: Memory) -> TestClient:
    bot = Agent(name="bot", model=ScriptedProvider([text("hi")]), plugins=[mem])
    plain = Agent(name="plain", model=ScriptedProvider([text("hi")]))
    app = create_app(
        {"bot": bot, "plain": plain},
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    return TestClient(app)


# ------------------------------------------------------------- discovery -


def test_feature_flag_and_agent_info(client: TestClient) -> None:
    assert client.get("/api/info").json()["features"]["memory"] is True
    agents = {a["name"]: a["memory"] for a in client.get("/api/agents").json()}
    assert agents == {"bot": True, "plain": False}


def test_feature_flag_false_without_any_memory() -> None:
    app = create_app(
        {"solo": Agent(name="solo", model=ScriptedProvider([text("hi")]))},
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    c = TestClient(app)
    assert c.get("/api/info").json()["features"]["memory"] is False


def test_agent_without_memory_404s(client: TestClient) -> None:
    assert client.get("/api/memory", params={"agent": "plain"}).status_code == 404
    r = client.put("/api/memory", params={"agent": "plain"}, json={"content": "- x"})
    assert r.status_code == 404
    # Multiple agents registered → the agent must be named.
    assert client.get("/api/memory").status_code == 400
    assert client.get("/api/memory", params={"agent": "nope"}).status_code == 404


# ------------------------------------------------------------ round-trip -


def test_get_empty_notes(client: TestClient, mem: Memory) -> None:
    data = client.get("/api/memory", params={"agent": "bot"}).json()
    assert data == {"content": "", "used": 0, "budget": mem.notes_budget}


async def test_get_reflects_plugin_writes(client: TestClient, mem: Memory) -> None:
    await mem.remember("likes jazz")
    data = client.get("/api/memory", params={"agent": "bot"}).json()
    assert data["content"] == "- likes jazz"
    assert data["used"] == len("- likes jazz")


def test_put_normalizes_and_round_trips(client: TestClient, tmp_path: Path) -> None:
    body = "- uses  vim   daily\nstray prose line\n- USES VIM DAILY\n- speaks French\n"
    r = client.put("/api/memory", params={"agent": "bot"}, json={"content": body})
    assert r.status_code == 200
    data = r.json()
    # Canonical form: bullets only, whitespace collapsed, case-insensitive dedup.
    assert data["content"] == "- uses vim daily\n- speaks French"
    assert data["used"] == len(data["content"])

    again = client.get("/api/memory", params={"agent": "bot"}).json()
    assert again == data

    # The default store is the human-editable MEMORY.md under the plugin root.
    assert (tmp_path / "mem" / "MEMORY.md").read_text() == data["content"]

    # An empty body clears the notes.
    wiped = client.put(
        "/api/memory", params={"agent": "bot"}, json={"content": ""}
    ).json()
    assert wiped["content"] == "" and wiped["used"] == 0
