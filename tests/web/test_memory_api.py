"""Tests for the /api/memory endpoints (the sidebar Memory editor).

The routes are a thin shell over ``Memory.notes_body`` / ``Memory.replace_notes``
(policy is tested with the plugin); here we pin discovery (feature flag +
per-agent ``memory`` flag), the GET/PUT round-trip with its meter fields, and
the 404 shape for agents without the plugin.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.plugins.memory import Memory  # noqa: E402
from lovia.plugins.memory import plugin as plugin_mod  # noqa: E402
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
    assert data == {
        "content": "",
        "used": 0,
        "budget": mem.notes_budget,
        "dreamed_at": None,
    }


async def test_get_reflects_plugin_writes(
    client: TestClient, mem: Memory, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    await mem.remember("likes jazz")
    line = "- [2026-01] likes jazz"
    data = client.get("/api/memory", params={"agent": "bot"}).json()
    assert data["content"] == line
    assert data["used"] == len(line)


def test_put_normalizes_and_round_trips(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    body = "- uses  vim   daily\nstray prose line\n- USES VIM DAILY\n- speaks French\n"
    r = client.put("/api/memory", params={"agent": "bot"}, json={"content": body})
    assert r.status_code == 200
    data = r.json()
    # Canonical form: bullets only, whitespace collapsed, keyed dedup, and a
    # [YYYY-MM] stamp on lines the editor added without one.
    assert data["content"] == "- [2026-01] uses vim daily\n- [2026-01] speaks French"
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


# ----------------------------------------------------------------- dream -


async def test_dream_endpoint_tidies_and_reports(
    client: TestClient, mem: Memory, monkeypatch
) -> None:
    async def fake_dream(body, max_chars, model):
        return ["[2026-01] merged note"]

    monkeypatch.setattr(plugin_mod, "_dream", fake_dream)
    await mem.remember("fact one")
    await mem.remember("fact two")

    data = client.post("/api/memory/dream", params={"agent": "bot"}).json()
    assert (data["before"], data["after"]) == (2, 1)
    assert data["content"] == "- [2026-01] merged note"
    assert data["used"] == len(data["content"])
    assert data["dreamed_at"] is not None

    # The editor's follow-up GET sees the tidied notes and the timestamp.
    again = client.get("/api/memory", params={"agent": "bot"}).json()
    assert again["content"] == data["content"]
    assert again["dreamed_at"] == data["dreamed_at"]


def test_dream_endpoint_404s_without_memory(client: TestClient) -> None:
    assert (
        client.post("/api/memory/dream", params={"agent": "plain"}).status_code == 404
    )


# ------------------------------------------------------------- shutdown -


def test_shutdown_drains_background_curation(tmp_path: Path, monkeypatch) -> None:
    # A clean server stop must not drop the last run's curation: the digest
    # below is still sleeping when the lifespan shutdown begins, and only the
    # drain in create_app's lifespan gets it onto disk.
    async def slow_digest(entries, current, model):
        await asyncio.sleep(0.3)
        return plugin_mod._RunDigest(facts=["survives shutdown"], summary="")

    monkeypatch.setattr(plugin_mod, "_digest", slow_digest)
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    mem = Memory(tmp_path / "mem", index=None, curate_in_background=True)
    bot = Agent(name="bot", model=ScriptedProvider([text("hi")]), plugins=[mem])
    app = create_app({"bot": bot}, store=ChatStore.in_memory(), generate_titles=False)

    with TestClient(app) as client:  # the context manager runs the lifespan
        assert client.post("/api/chat", json={"message": "hello"}).status_code == 200
    body = (tmp_path / "mem" / "MEMORY.md").read_text()
    assert body == "- [2026-01] survives shutdown"
