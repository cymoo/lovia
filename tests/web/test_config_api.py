"""Tests for /api/config and runtime reconfiguration (``ConfigRuntime``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia.web import create_app  # noqa: E402
from lovia.web.__main__ import build_parser  # noqa: E402
from lovia.web.config import (  # noqa: E402
    ConfigRuntime,
    LoadedConfig,
    ValidationOutcome,
    WebConfig,
    storage,
)
from lovia.web.store import ChatStore  # noqa: E402


@pytest.fixture
def served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[TestClient, ConfigRuntime, Any]]:
    """An unconfigured app serving the config API, cwd-isolated to tmp."""
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["--no-memory", "--no-subagents"])
    loaded = LoadedConfig(
        WebConfig(),
        storage.project_config_path(),
        storage.PROJECT_CONFIG_LABEL,
        exists=False,
    )
    runtime = ConfigRuntime(
        args=args, loaded=loaded, store=ChatStore.in_memory(), question_channel=None
    )
    app = create_app(
        {}, store=runtime.store, config_runtime=runtime, generate_titles=False
    )
    # base_url localhost => a loopback Host header, passing the rebinding guard.
    with TestClient(app, base_url="http://localhost") as client:
        yield client, runtime, app


def _add_model(client: TestClient, **fields: object) -> dict:
    payload: dict[str, object] = {
        "model": "deepseek-v4-pro",
        "base_url": "https://gw.example/v1",
        "api_key": "sk-secret-1234567890",
    }
    payload.update(fields)
    res = client.post("/api/config/models", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


# ------------------------------------------------------------ lifecycle -


def test_unconfigured_boot_reports_and_blocks_chat(served) -> None:
    client, runtime, _app = served
    info = client.get("/api/info").json()
    assert info["configured"] is False
    assert info["agents"] == []
    assert info["features"]["model_config"] is True
    cfg = client.get("/api/config").json()
    assert cfg["configured"] is False
    assert cfg["missing"] == ["model"]
    # No agent to chat with yet.
    res = client.post("/api/chat", json={"session_id": "s", "message": "hi"})
    assert res.status_code in (400, 404, 422)


def test_first_model_brings_the_agent_up(served, tmp_path: Path) -> None:
    client, runtime, app = served
    out = _add_model(client, name="DeepSeek")
    assert out["id"] == "deepseek"
    info = client.get("/api/info").json()
    assert info["configured"] is True
    assert info["agents"] == ["lovia"]
    assert runtime.configured
    # The document was persisted to the runtime's target scope.
    saved = json.loads((tmp_path / ".lovia" / "config.json").read_text())
    assert saved["roles"]["chat"] == "deepseek"
    assert saved["models"][0]["api_key"] == "sk-secret-1234567890"


def test_get_config_masks_secrets_and_never_caches(served) -> None:
    client, _runtime, _app = served
    _add_model(client)
    res = client.get("/api/config")
    assert res.headers["cache-control"] == "no-store"
    profile = res.json()["models"][0]
    assert profile["api_key"] == {"set": True, "hint": "sk-…7890"}
    assert "sk-secret" not in res.text


def test_embedder_apps_have_no_config_api() -> None:
    from lovia.agent import Agent

    from ..scripted_provider import ScriptedProvider, text

    app = create_app(
        Agent(name="bot", model=ScriptedProvider([text("hi")])),
        session=None,
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    with TestClient(app) as client:
        info = client.get("/api/info").json()
        assert info["configured"] is True
        assert info["features"]["model_config"] is False
        assert client.get("/api/config").status_code == 404


# ----------------------------------------------------------------- CRUD -


def test_create_rejects_duplicate_and_bad_vendor(served, tmp_path: Path) -> None:
    client, _runtime, _app = served
    _add_model(client, id="one")
    assert (
        client.post("/api/config/models", json={"id": "one", "model": "x"}).status_code
        == 409
    )
    # An unknown vendor prefix fails the pre-persist build: 400, nothing saved.
    before = (tmp_path / ".lovia" / "config.json").read_text()
    res = client.put("/api/config/models/one", json={"model": "nosuchvendor:m"})
    assert res.status_code == 400
    assert "Unknown model spec" in res.json()["detail"]
    assert (tmp_path / ".lovia" / "config.json").read_text() == before


def test_update_key_keep_replace_clear(served) -> None:
    client, runtime, _app = served
    _add_model(client, id="m")

    def stored_key() -> str | None:
        return runtime.config.profile("m").api_key  # type: ignore[union-attr]

    base = {"model": "deepseek-v4-pro", "base_url": "https://gw.example/v1"}
    # api_key absent/None -> keep
    assert client.put("/api/config/models/m", json=base).status_code == 200
    assert stored_key() == "sk-secret-1234567890"
    # replace
    client.put("/api/config/models/m", json={**base, "api_key": "sk-new-key-000011"})
    assert stored_key() == "sk-new-key-000011"
    # "" -> clear (a keyless gateway)
    client.put("/api/config/models/m", json={**base, "api_key": ""})
    assert stored_key() is None
    assert client.put("/api/config/models/nope", json=base).status_code == 404


def test_delete_guards_the_default_and_nulls_role_refs(served) -> None:
    client, runtime, _app = served
    _add_model(client, id="chat-m")
    _add_model(client, id="eyes", model="glm-4.6v", vision="on")
    client.put("/api/config/roles", json={"vision": "eyes"})
    assert client.delete("/api/config/models/chat-m").status_code == 409
    assert client.delete("/api/config/models/nope").status_code == 404
    res = client.delete("/api/config/models/eyes")
    assert res.status_code == 200
    assert runtime.config.roles.vision is None
    assert [p.id for p in runtime.config.models] == ["chat-m"]


# ---------------------------------------------------------------- roles -


def test_role_switch_hot_swaps_the_served_agent(served) -> None:
    client, _runtime, app = served
    _add_model(client, id="a")
    _add_model(client, id="b", model="openai:gpt-5.5", api_key="sk-b-abcdefgh")
    deps = app.state.deps
    old = deps.agents["lovia"]
    events: list[tuple[str, dict]] = []
    deps.emit = lambda event, **data: events.append((event, data))  # type: ignore[method-assign]
    assert client.put("/api/config/roles", json={"chat": "b"}).status_code == 200
    new = deps.agents["lovia"]
    assert new is not old  # atomically replaced, old runs keep their copy
    assert getattr(new.model, "model", None) == "gpt-5.5"
    assert (
        "config_changed",
        {
            "configured": True,
            "model": "openai:gpt-5.5",
            "profile_id": "b",
            "name": "openai:gpt-5.5",
        },
    ) in events


def test_roles_reject_unknown_ref_and_cleared_chat(served) -> None:
    client, _runtime, _app = served
    _add_model(client, id="a")
    assert client.put("/api/config/roles", json={"chat": "nope"}).status_code == 400
    assert client.put("/api/config/roles", json={"chat": None}).status_code == 400
    # Absent fields stay untouched; explicit null clears an aux role.
    client.put("/api/config/roles", json={"aux": "a"})
    assert _config_roles(client)["aux"] == "a"
    client.put("/api/config/roles", json={"aux": None})
    assert _config_roles(client)["aux"] is None


def _config_roles(client: TestClient) -> dict:
    return client.get("/api/config").json()["roles"]


def test_vision_role_wires_see_image(served) -> None:
    client, _runtime, app = served
    # A text-only chat model (vision "off") plus an assigned vision profile.
    _add_model(client, id="blind", vision="off")
    _add_model(client, id="eyes", model="glm-4.6v", vision="on")
    client.put("/api/config/roles", json={"vision": "eyes"})
    agent = app.state.deps.agents["lovia"]
    assert "see_image" in {t.name for t in agent.tools}


# --------------------------------------------------------------- search -


def test_search_backend_rebuilds_tools(served) -> None:
    client, runtime, app = served
    _add_model(client)
    assert client.put("/api/config/search", json={"backend": "off"}).status_code == 200
    agent = app.state.deps.agents["lovia"]
    assert "web_search" not in {t.name for t in agent.tools}
    client.put(
        "/api/config/search",
        json={"backend": "tavily", "tavily_api_key": "tvly-secret-99"},
    )
    agent = app.state.deps.agents["lovia"]
    assert "web_search" in {t.name for t in agent.tools}
    out = client.get("/api/config").json()["search"]
    assert out == {
        "backend": "tavily",
        "tavily_api_key": {"set": True, "hint": "tvl…t-99"},
    }
    # "" clears the key.
    client.put("/api/config/search", json={"tavily_api_key": ""})
    assert runtime.config.search.tavily_api_key is None


# ----------------------------------------------------------------- test -


def test_probe_endpoint_free_form(served, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _runtime, _app = served
    seen: dict[str, object] = {}

    def fake_validate(conn, **kwargs):
        seen.update(model=conn.model, base_url=conn.base_url, key=conn.api_key)
        conn.context_window = 131_072
        conn.window_from_endpoint = True
        conn.available_models = ["deepseek-v4-pro", "deepseek-v4"]
        return ValidationOutcome.OK, "HTTP 200"

    monkeypatch.setattr("lovia.web.api.config.validate_connection", fake_validate)
    res = client.post(
        "/api/config/test",
        json={
            "model": "deepseek-v4",
            "base_url": "https://gw.example/v1",
            "api_key": "sk-t-123456",
        },
    )
    assert res.status_code == 200
    out = res.json()
    assert out["outcome"] == "ok"
    assert out["context_window"] == 131_072
    assert out["window_from_endpoint"] is True
    assert out["models"] == ["deepseek-v4-pro", "deepseek-v4"]
    assert out["note"] is None
    assert seen == {
        "model": "deepseek-v4",
        "base_url": "https://gw.example/v1",
        "key": "sk-t-123456",
    }


def test_probe_endpoint_reuses_the_stored_key(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The UI can test a saved profile without ever holding its secret."""
    client, _runtime, _app = served
    _add_model(client, id="m")
    seen: dict[str, object] = {}

    def fake_validate(conn, **kwargs):
        seen.update(key=conn.api_key)
        conn.available_models = ["other-model"]
        return ValidationOutcome.OK, "HTTP 200"

    monkeypatch.setattr("lovia.web.api.config.validate_connection", fake_validate)
    res = client.post("/api/config/test", json={"profile_id": "m"})
    assert res.status_code == 200
    assert seen["key"] == "sk-secret-1234567890"  # stored, not round-tripped
    assert "does not list" in (res.json()["note"] or "")
    assert (
        client.post("/api/config/test", json={"profile_id": "nope"}).status_code == 404
    )
    assert client.post("/api/config/test", json={}).status_code == 400


# ------------------------------------------------------------- security -


def test_writes_refuse_foreign_hosts_without_auth(served) -> None:
    client, _runtime, _app = served
    res = client.post(
        "/api/config/models",
        json={"model": "x"},
        headers={"host": "evil.example"},
    )
    assert res.status_code == 403
    # Reads stay host-agnostic (they leak nothing beyond masked hints).
    assert (
        client.get("/api/config", headers={"host": "evil.example"}).status_code == 200
    )


def test_token_auth_lifts_the_host_restriction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["--no-memory", "--no-subagents"])
    loaded = LoadedConfig(
        WebConfig(),
        storage.project_config_path(),
        storage.PROJECT_CONFIG_LABEL,
        exists=False,
    )
    runtime = ConfigRuntime(
        args=args, loaded=loaded, store=ChatStore.in_memory(), question_channel=None
    )
    app = create_app(
        {},
        store=runtime.store,
        config_runtime=runtime,
        generate_titles=False,
        token="sesame",
    )
    assert runtime.require_local_host is False
    with TestClient(app, base_url="http://proxy.example") as client:
        headers = {"Authorization": "Bearer sesame"}
        assert (
            client.post(
                "/api/config/models",
                json={"model": "deepseek-v4-pro", "base_url": "https://gw/v1"},
                headers=headers,
            ).status_code
            == 201
        )
        # ...but the token itself is still required.
        assert client.get("/api/config").status_code == 401
