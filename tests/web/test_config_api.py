"""Tests for /api/config and runtime reconfiguration (``ConfigRuntime``)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Skills  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.builder import Deployment  # noqa: E402
from lovia.web.config import (  # noqa: E402
    AgentConfig,
    ConfigRuntime,
    LoadedConfig,
    ValidationOutcome,
    WebConfig,
    storage,
)
from lovia.web.store import ChatStore  # noqa: E402


def _lean() -> WebConfig:
    """A fresh document with memory and subagents off (no disk, fewer parts)."""
    return WebConfig(agents=[AgentConfig(id="lovia", memory=False, subagents=False)])


def _loaded(tmp_path: Path) -> LoadedConfig:
    """``_lean()``, not saved yet, in a home under ``tmp_path``."""
    path = storage.config_path(tmp_path / "home")
    return LoadedConfig(_lean(), path, storage.display_path(path), exists=False)


@pytest.fixture
def served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[TestClient, ConfigRuntime, Any]]:
    """An unconfigured app serving the config API, cwd-isolated to tmp."""
    monkeypatch.chdir(tmp_path)
    loaded = _loaded(tmp_path)
    runtime = ConfigRuntime(
        deployment=Deployment(), loaded=loaded, store=ChatStore.in_memory()
    )
    app = create_app({}, store=runtime.store, config_runtime=runtime, titles=False)
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
    saved = json.loads((tmp_path / "home" / "config.json").read_text())
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


def test_create_app_rejects_empty_agents_without_a_runtime() -> None:
    """An empty mapping without /api/config could never gain an agent."""
    with pytest.raises(ValueError, match="config_runtime"):
        create_app({}, store=ChatStore.in_memory())


def test_embedder_apps_have_no_config_api() -> None:
    from lovia.agent import Agent

    from ..scripted_provider import ScriptedProvider, text

    app = create_app(
        Agent(name="bot", model=ScriptedProvider([text("hi")])),
        session=None,
        store=ChatStore.in_memory(),
        titles=False,
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
    before = (tmp_path / "home" / "config.json").read_text()
    res = client.put("/api/config/models/one", json={"model": "nosuchvendor:m"})
    assert res.status_code == 400
    assert res.json()["detail"]["code"] == "invalid_request"
    assert "Unknown model spec" in res.json()["detail"]["message"]
    assert (tmp_path / "home" / "config.json").read_text() == before


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


def test_extra_body_round_trips_and_reaches_the_served_provider(served) -> None:
    client, runtime, app = served
    extras = {"reasoning_effort": "medium", "stream_options": None}
    _add_model(client, id="m", extra_body=extras)

    # Stored and echoed as written — a null value is a removal, not absence.
    assert client.get("/api/config").json()["models"][0]["extra_body"] == extras
    assert runtime.config.profile("m").extra_body == extras  # type: ignore[union-attr]
    assert app.state.deps.agents["lovia"].model._extra_body == extras

    # An update replaces the whole object; omitting the field clears it.
    base = {"model": "deepseek-v4-pro", "base_url": "https://gw.example/v1"}
    client.put("/api/config/models/m", json={**base, "extra_body": {"top_k": 1}})
    assert app.state.deps.agents["lovia"].model._extra_body == {"top_k": 1}
    client.put("/api/config/models/m", json=base)
    assert app.state.deps.agents["lovia"].model._extra_body == {}


def test_extra_body_rejects_runtime_owned_fields_and_non_objects(served) -> None:
    client, _runtime, _app = served
    res = client.post(
        "/api/config/models",
        json={"model": "m", "extra_body": {"messages": [], "reasoning_effort": "low"}},
    )
    assert res.status_code == 400
    assert "messages" in res.text
    res = client.post("/api/config/models", json={"model": "m", "extra_body": []})
    assert res.status_code == 422


def test_aux_role_serves_titles_and_followups_with_its_extras(served) -> None:
    client, _runtime, app = served
    _add_model(client, id="chat")
    _add_model(
        client,
        id="quiet",
        model="openai:gpt-small",
        api_key="sk-quiet-abcdefgh",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    client.put("/api/config/roles", json={"aux": "quiet"})
    deps = app.state.deps
    assert deps.title_model is deps.followup_model
    assert getattr(deps.title_model, "model", None) == "gpt-small"
    assert deps.title_model._extra_body == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    retired = deps.title_model
    closed = threading.Event()

    async def aclose() -> None:
        closed.set()

    retired.aclose = aclose  # type: ignore[method-assign]
    client.put("/api/config/roles", json={"aux": None})
    assert deps.title_model is None and deps.followup_model is None
    # The replaced aux provider is retired like the main one (after the
    # grace period for a title/follow-up call caught mid-swap).
    assert closed.wait(timeout=5.0)


def test_duplicate_copies_the_profile_with_its_key(served) -> None:
    client, runtime, _app = served
    _add_model(client, id="m", name="Main", extra_body={"reasoning_effort": "high"})
    res = client.post("/api/config/models/m/duplicate")
    assert res.status_code == 201, res.text
    assert res.json()["id"] == "m-copy"
    copy = runtime.config.profile("m-copy")
    assert copy is not None
    assert copy.name == "Main copy"
    assert copy.api_key == "sk-secret-1234567890"  # never left the server
    assert copy.extra_body == {"reasoning_effort": "high"}
    assert runtime.config.roles.chat == "m"  # roles untouched
    # A second copy gets a unique id; unknown ids 404.
    assert client.post("/api/config/models/m/duplicate").json()["id"] == "m-copy-2"
    assert client.post("/api/config/models/nope/duplicate").status_code == 404


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
    deps.emit = lambda event, data: events.append((event, dict(data)))  # type: ignore[method-assign]
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


def test_vision_role_wires_describe_image(served) -> None:
    client, _runtime, app = served
    # A text-only chat model (vision "off") plus an assigned vision profile.
    _add_model(client, id="blind", vision="off")
    _add_model(client, id="eyes", model="glm-4.6v", vision="on")
    client.put("/api/config/roles", json={"vision": "eyes"})
    agent = app.state.deps.agents["lovia"]
    assert "describe_image" in {t.name for t in agent.tools}


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


# --------------------------------------------------------------- skills -


def _write_skill(
    root: Path, name: str, description: str = "Does a useful thing."
) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nBody.\n",
        encoding="utf-8",
    )


def test_skills_block_in_get_config(served) -> None:
    client, _runtime, _app = served
    out = client.get("/api/config").json()["skills"]
    assert out == {"dirs": ["~/.agents/skills"]}


def test_skills_update_rebuilds_the_agent(
    served, tmp_path: Path, fake_home: Path
) -> None:
    client, runtime, app = served
    _write_skill(fake_home / ".agents" / "skills", "greet")
    _add_model(client)
    agent = app.state.deps.agents["lovia"]
    assert any(isinstance(p, Skills) for p in agent.plugins)
    res = client.put("/api/config/skills", json={"dirs": []})
    assert res.status_code == 200
    assert res.json()["config"]["skills"] == {"dirs": []}
    agent = app.state.deps.agents["lovia"]
    assert not any(isinstance(p, Skills) for p in agent.plugins)
    saved = json.loads((tmp_path / "home" / "config.json").read_text())
    assert saved["skills"] == {"dirs": []}
    assert runtime.config.skills.dirs == []


def test_skills_missing_dir_still_applies(served) -> None:
    # A nonexistent path is a warn-and-skip at build time, never a 400 —
    # otherwise a stale config.json could brick the hot-swap and the boot.
    client, runtime, _app = served
    _add_model(client)
    res = client.put("/api/config/skills", json={"dirs": ["~/definitely-nope"]})
    assert res.status_code == 200
    assert runtime.config.skills.dirs == ["~/definitely-nope"]


def test_skills_blank_entry_is_rejected(served) -> None:
    client, _runtime, _app = served
    _add_model(client)
    res = client.put("/api/config/skills", json={"dirs": ["  "]})
    assert res.status_code == 400
    message = res.json()["detail"]["message"]
    assert "skills" in message
    assert "non-empty" in message


def test_skills_scan_reports_each_root(served, tmp_path: Path) -> None:
    client, _runtime, _app = served
    project = tmp_path / ".agents" / "skills"
    _write_skill(project, "greet")
    broken = project / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: broken\n---\nBody.\n", encoding="utf-8"
    )
    team = tmp_path / "team-skills"
    _write_skill(team, "greet", "A shadowed duplicate.")
    _add_model(client)
    dirs = [str(project), "~/.agents/skills", str(team)]
    assert client.put("/api/config/skills", json={"dirs": dirs}).status_code == 200

    res = client.get("/api/config/skills")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    out = res.json()
    assert out["defaults"] == ["~/.agents/skills"]
    roots = {r["path"]: r for r in out["roots"]}
    assert set(roots) == set(dirs)

    first = roots[str(project)]
    assert first["kind"] == "custom"
    assert first["exists"] is True
    assert [s["name"] for s in first["skills"]] == ["greet"]
    assert first["skills"][0]["shadowed"] is False
    assert first["skills"][0]["description"] == "Does a useful thing."
    assert first["problems"][0]["dir"] == "broken"
    assert "description" in first["problems"][0]["error"]

    user_root = roots["~/.agents/skills"]
    assert user_root["kind"] == "default"
    assert user_root["exists"] is False
    assert user_root["skills"] == []

    team_root = roots[str(team)]
    assert team_root["kind"] == "custom"
    assert [(s["name"], s["shadowed"]) for s in team_root["skills"]] == [
        ("greet", True)
    ]


def test_skills_scan_keeps_identity_duplicates_visible(served, tmp_path: Path) -> None:
    # Two spellings of one directory stay two removable rows; only the
    # plugin roots are deduplicated.
    client, _runtime, _app = served
    _write_skill(tmp_path / "x", "greet")
    _add_model(client)
    dirs = [str(tmp_path / "x"), f"{tmp_path}{os.sep}.{os.sep}x"]
    assert client.put("/api/config/skills", json={"dirs": dirs}).status_code == 200
    roots = client.get("/api/config/skills").json()["roots"]
    assert [r["path"] for r in roots] == dirs
    assert [s["shadowed"] for s in roots[1]["skills"]] == [True]


def test_skills_refuse_a_relative_directory(served) -> None:
    client, runtime, _app = served
    _add_model(client)
    for entry in ("team-skills", ".agents/skills"):
        res = client.put("/api/config/skills", json={"dirs": [entry]})
        assert res.status_code == 400, res.text
        assert "loads by itself" in res.json()["detail"]["hint"]
    assert runtime.config.skills.dirs == ["~/.agents/skills"]


def test_skills_scan_lists_no_project_skills(served, tmp_path: Path) -> None:
    # The launch directory's own skills belong to its chats, not Settings.
    client, _runtime, _app = served
    _write_skill(tmp_path / ".agents" / "skills", "greet")
    _add_model(client)
    roots = client.get("/api/config/skills").json()["roots"]
    assert [r["path"] for r in roots] == ["~/.agents/skills"]


def test_skills_scan_works_unconfigured(served, fake_home: Path) -> None:
    # The pane must render (read-only) before any model exists.
    client, _runtime, _app = served
    _write_skill(fake_home / ".agents" / "skills", "greet")
    res = client.get("/api/config/skills")
    assert res.status_code == 200
    (root,) = res.json()["roots"]
    assert [s["name"] for s in root["skills"]] == ["greet"]


def test_skills_write_refuses_foreign_hosts(served) -> None:
    client, _runtime, _app = served
    res = client.put(
        "/api/config/skills",
        json={"dirs": []},
        headers={"host": "evil.example"},
    )
    assert res.status_code == 403


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
    # An explicit "" means "probe keyless" — it must never silently
    # substitute the stored secret (the base URL is caller-chosen).
    client.post(
        "/api/config/test",
        json={"profile_id": "m", "api_key": "", "base_url": "https://elsewhere/v1"},
    )
    assert seen["key"] is None
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
    loaded = _loaded(tmp_path)
    runtime = ConfigRuntime(
        deployment=Deployment(), loaded=loaded, store=ChatStore.in_memory()
    )
    app = create_app(
        {},
        store=runtime.store,
        config_runtime=runtime,
        titles=False,
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


# --------------------------------------------------------------- agents -


def _served_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deployment: Deployment
) -> tuple[TestClient, ConfigRuntime]:
    monkeypatch.chdir(tmp_path)
    loaded = _loaded(tmp_path)
    runtime = ConfigRuntime(
        deployment=deployment, loaded=loaded, store=ChatStore.in_memory()
    )
    app = create_app({}, store=runtime.store, config_runtime=runtime, titles=False)
    return TestClient(app, base_url="http://localhost"), runtime


def _add_agent(client: TestClient, **fields: object) -> str:
    res = client.post("/api/config/agents", json=fields)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def test_run_settings_apply_to_the_next_run(served) -> None:
    from lovia.web.followups import FollowupRequest

    client, runtime, app = served
    _add_model(client)
    deps = app.state.deps
    res = client.put(
        "/api/config/agents/lovia",
        json={"max_turns": 7, "max_retries": 2, "followups": False},
    )
    assert res.status_code == 200, res.text
    (agent,) = res.json()["config"]["agents"]
    assert (agent["max_turns"], agent["max_retries"], agent["followups"]) == (
        7,
        2,
        False,
    )
    # Read by every run at its start: the next one gets these.
    assert deps.max_turns_for("lovia") == 7
    assert deps.agents["lovia"].retry.max_attempts == 3
    asked = FollowupRequest(session_id="s", agent="lovia")
    assert await runtime.followups(asked) == []
    # An explicit null restores the core default retry posture.
    client.put("/api/config/agents/lovia", json={"max_retries": None})
    assert deps.agents["lovia"].retry.max_attempts == 5
    assert deps.max_turns_for("lovia") == 7


def test_the_page_learns_what_unset_values_mean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia.web import WorkspaceSwitch

    client, runtime = _served_with(monkeypatch, tmp_path, Deployment(readonly=True))
    client.app.state.deps.workspace_switch = WorkspaceSwitch(modes=("readonly",))
    with client:
        # Retries left empty: the core default, as a number to show.
        assert client.get("/api/config").json()["defaults"]["max_retries"] == 4
        # A chat with no mode stored runs the CLI agent's own workspace —
        # built in the default mode as this server runs it: name it.
        modes = client.get("/api/info").json()["workspace_modes"]
        assert modes["own"] == modes["default"] == "readonly"


def test_run_settings_reject_nulls_and_out_of_range(served) -> None:
    client, *_ = served
    url = "/api/config/agents/lovia"
    assert client.put(url, json={"max_turns": None}).status_code == 400
    assert client.put(url, json={"max_turns": 0}).status_code == 422
    assert client.put(url, json={"memory": None}).status_code == 400
    # Null where it means "inherit" is fine.
    assert client.put(url, json={"model": None, "skills": None}).status_code == 200
    assert client.put("/api/config/agents/nope", json={}).status_code == 404


def test_mode_change_rebuilds_the_agent_and_spares_a_held_copy(served) -> None:
    client, runtime, app = served
    _add_model(client)
    deps = app.state.deps
    held = deps.agents["lovia"]  # what an in-flight run would be holding
    assert held.workspace.policy.write == "allow"  # coding by default
    res = client.put("/api/config/agents/lovia", json={"mode": "readonly"})
    assert res.status_code == 200, res.text
    (agent,) = res.json()["config"]["agents"]
    assert agent["mode"] == agent["effective_mode"] == "readonly"
    assert deps.agents["lovia"].workspace.policy.write == "deny"
    assert held.workspace.policy.write == "allow"  # untouched
    saved = json.loads(runtime.loaded.path.read_text())
    assert saved["agents"][0]["mode"] == "readonly"
    # What a new chat with it starts in.
    assert client.get("/api/agents/lovia").json()["mode"] == "readonly"


def test_auto_allow_change_reaches_the_command_policy(served) -> None:
    client, _runtime, app = served
    _add_model(client)
    deps = app.state.deps
    assert deps.agents["lovia"].workspace.policy.decide_command("ls").action == "allow"
    res = client.put("/api/config/agents/lovia", json={"auto_allow": []})
    assert res.status_code == 200, res.text
    assert deps.agents["lovia"].workspace.policy.decide_command("ls").action == "ask"


def test_agent_settings_apply(served, tmp_path: Path) -> None:
    client, _runtime, app = served
    _add_model(client)
    res = client.put(
        "/api/config/agents/lovia",
        json={"instructions": "Answer in haiku.", "subagents": True, "search": False},
    )
    assert res.status_code == 200, res.text
    agent = app.state.deps.agents["lovia"]
    assert agent.instructions.startswith("Answer in haiku.")
    assert any(type(p).__name__ == "Subagents" for p in agent.plugins)
    tools = {t.name for t in agent.tools}
    assert "web_search" not in tools and "web_fetch" in tools


def test_agents_are_served_side_by_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from lovia.plugins import Memory

    deployment = Deployment(workspace=str(tmp_path), home=tmp_path / "home")
    client, runtime = _served_with(monkeypatch, tmp_path, deployment)
    with client:
        _add_model(client)
        client.put("/api/config/agents/lovia", json={"memory": True})
        new = _add_agent(
            client,
            name="Code Review",
            description="Reads, never writes",
            mode="readonly",
        )
        assert new == "code-review"
        deps = client.app.state.deps
        assert list(deps.agents) == ["lovia", "code-review"]
        lovia, review = deps.agents["lovia"], deps.agents["code-review"]
        # The core name is the id; the label is the page's.
        assert review.name == "code-review"
        assert review.workspace.policy.write == "deny"
        # One model, one provider; one memory, one plugin (it owns the lock).
        assert lovia.model is review.model
        mems = [p for a in (lovia, review) for p in a.plugins if isinstance(p, Memory)]
        assert len(mems) == 2 and mems[0] is mems[1]
        info = {a["name"]: a for a in client.get("/api/agents").json()}
        assert info["code-review"]["label"] == "Code Review"
        assert info["code-review"]["description"] == "Reads, never writes"
        assert info["code-review"]["mode"] == "readonly"
        assert info["lovia"]["mode"] == "coding"
        # A name with nothing to slug gets a generic id, kept unique.
        assert _add_agent(client, name="审阅") == "agent"
        assert _add_agent(client, name="审阅") == "agent-2"
        # Any name makes a valid id: no leading punctuation, room for the suffix.
        assert _add_agent(client, name="_Review") == "review"
        long = _add_agent(client, name="a" * 65)
        assert len(long) <= 64
        assert len(_add_agent(client, name="a" * 65)) <= 64
        assert client.post(f"/api/config/agents/{long}/duplicate").status_code == 201


def test_a_background_child_runs_on_its_agents_turn_limit(served) -> None:
    from lovia.plugins import Subagents

    client, _runtime, app = served
    _add_model(client)
    res = client.put(
        "/api/config/agents/lovia", json={"max_turns": 1, "subagents": True}
    )
    assert res.status_code == 200, res.text
    deps = app.state.deps
    (plugin,) = [p for p in deps.agents["lovia"].plugins if isinstance(p, Subagents)]
    (child,) = plugin.agents
    # A child run goes by its own name (deps.name_of finds no registry key).
    assert deps.name_of(child) == child.name != "lovia"
    assert deps.max_turns_for(child.name) == 1


def test_the_build_hooks_use_each_agents_own_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    deployment = Deployment(workspace=str(tmp_path), home=tmp_path / "home")
    client, runtime = _served_with(monkeypatch, tmp_path, deployment)
    with client:
        _add_model(client)
        _add_agent(client, name="strict", auto_allow=[], instructions="Be strict.")
        deps = client.app.state.deps
        strict, lovia = deps.agents["strict"], deps.agents["lovia"]
        built = runtime.workspace_for(strict, tmp_path, "coding")
        assert built.policy.decide_command("ls").action == "ask"
        built = runtime.workspace_for(lovia, tmp_path, "coding")
        assert built.policy.decide_command("ls").action == "allow"
        # A background child runs on its agent's settings.
        child = strict.clone(name="strict.sub")
        assert runtime.agent_config(child.name).id == "strict"
        assert runtime.agent_for(child, tmp_path).instructions.startswith("Be strict.")
        # An agent deleted since its run began runs on as the default does.
        client.delete("/api/config/agents/strict")
        built = runtime.workspace_for(strict, tmp_path, "coding")
        assert built.policy.decide_command("ls").action == "allow"


def test_default_duplicate_and_delete(served) -> None:
    from lovia.web.store import ChatMeta

    client, runtime, app = served
    _add_model(client)
    _add_agent(client, name="writer", instructions="Write.")
    deps = app.state.deps
    res = client.post("/api/config/agents/writer/default")
    assert res.status_code == 200, res.text
    assert [a["id"] for a in res.json()["config"]["agents"]] == ["writer", "lovia"]
    assert client.get("/api/info").json()["default_agent"] == "writer"
    copy = client.post("/api/config/agents/writer/duplicate")
    assert copy.status_code == 201, copy.text
    assert copy.json()["id"] == "writer-copy"
    assert runtime.config.agent("writer-copy").instructions == "Write."
    # A chat whose agent was deleted runs on the default from then on.
    assert client.delete("/api/config/agents/lovia").status_code == 200
    orphan = ChatMeta(id="s", title=None, agent="lovia", created_at=0, updated_at=0)
    assert deps.agent_of(orphan) == "writer"
    client.delete("/api/config/agents/writer-copy")
    res = client.delete("/api/config/agents/writer")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "agent_in_use"


def test_an_agent_names_a_configured_model(served) -> None:
    client, runtime, app = served
    _add_model(client, name="main")
    # An official host with no key: fine to save, not to run an agent on.
    _add_model(client, name="bare", model="gpt-x", base_url=None, api_key=None)
    res = client.put("/api/config/agents/lovia", json={"model": "nope"})
    assert res.status_code == 400
    res = client.put("/api/config/agents/lovia", json={"model": "bare"})
    assert res.status_code == 400
    assert "lovia" in res.json()["detail"]["message"]
    _add_model(client, name="other", model="other-model")
    res = client.put("/api/config/agents/lovia", json={"model": "other"})
    assert res.status_code == 200, res.text
    assert app.state.deps.agents["lovia"].model.model == "other-model"
    # Deleting the model it runs on: it follows the default chat model again.
    assert client.delete("/api/config/models/other").status_code == 200
    assert runtime.config.default_agent.model is None


def test_agent_skills_replace_the_global_directories(served, tmp_path: Path) -> None:
    client, runtime, app = served
    _add_model(client)
    own = tmp_path / "own-skills"
    own.mkdir()
    res = client.put("/api/config/agents/lovia", json={"skills": [str(own)]})
    assert res.status_code == 200, res.text
    (skills,) = [
        p for p in app.state.deps.agents["lovia"].plugins if isinstance(p, Skills)
    ]
    assert skills is not None
    res = client.put("/api/config/agents/lovia", json={"skills": [".agents/skills"]})
    assert res.status_code == 400


def test_max_tokens_round_trips_through_a_profile(served) -> None:
    client, _runtime, app = served
    out = _add_model(client, max_tokens=4096)
    model = out["config"]["models"][0]
    assert model["max_tokens"] == 4096
    assert app.state.deps.agents["lovia"].settings.max_tokens == 4096


@pytest.mark.parametrize(
    ("deployment", "lock"),
    [
        (Deployment(readonly=True), "readonly"),
        (Deployment(workspace=None), "no_workspace"),
    ],
)
def test_locked_workspace_settings_refuse_server_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deployment: Deployment, lock: str
) -> None:
    client, runtime = _served_with(monkeypatch, tmp_path, deployment)
    url = "/api/config/agents/lovia"
    with client:
        assert client.get("/api/config").json()["permissions"]["lock"] == lock
        res = client.put(url, json={"mode": "readonly"})
        assert res.status_code == 403
        assert res.json()["detail"]["code"] == "workspace_locked"
        res = client.post("/api/config/agents", json={"auto_allow": []})
        assert res.status_code == 403
        # Other settings stay editable from the browser, the unchanged
        # permissions a whole-form save sends back included.
        res = client.put(url, json={"max_turns": 9, "mode": "coding"})
        assert res.status_code == 200, res.text
    assert runtime.config.default_agent.mode == "coding"


def test_readonly_flag_shows_the_effective_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _runtime = _served_with(monkeypatch, tmp_path, Deployment(readonly=True))
    with client:
        cfg = client.get("/api/config").json()
    # The file keeps the user's choice; runs get what the flag allows.
    (agent,) = cfg["agents"]
    assert agent["mode"] == "coding"
    assert agent["effective_mode"] == "readonly"
    assert cfg["permissions"]["modes"] == ["readonly"]


def test_settings_offer_every_mode_without_a_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _runtime = _served_with(monkeypatch, tmp_path, Deployment())
    with client:
        permissions = client.get("/api/config").json()["permissions"]
        assert permissions["modes"] == ["readonly", "coding", "trusted"]
        assert permissions["lock"] is None
        res = client.put("/api/config/agents/lovia", json={"mode": "trusted"})
        assert res.status_code == 200, res.text


def test_read_only_config_boots_and_refuses_every_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, runtime = _served_with(monkeypatch, tmp_path, Deployment())
    monkeypatch.setattr(storage, "config_writable", lambda path: False)
    monkeypatch.setattr("lovia.web.config.runtime.config_writable", lambda path: False)
    with client:
        cfg = client.get("/api/config").json()
        assert cfg["writable"] is False
        for method, url, body in (
            ("post", "/api/config/models", {"model": "m"}),
            ("post", "/api/config/agents", {"name": "x"}),
            ("put", "/api/config/agents/lovia", {"memory": False}),
            ("delete", "/api/config/agents/lovia", None),
            ("put", "/api/config/search", {"backend": "off"}),
            ("put", "/api/config/skills", {"dirs": []}),
        ):
            res = client.request(method.upper(), url, json=body)
            assert res.status_code == 409, url
            assert res.json()["detail"]["code"] == "config_read_only"
    assert not runtime.loaded.path.exists()


def test_config_writable_follows_file_and_directory(tmp_path: Path) -> None:
    import os
    import stat

    target = tmp_path / "a" / "b" / "config.json"
    assert storage.config_writable(target)  # nearest existing dir is writable
    target.parent.mkdir(parents=True)
    target.write_text("{}")
    assert storage.config_writable(target)
    target.chmod(stat.S_IRUSR)
    try:
        if os.access(target, os.W_OK):  # pragma: no cover - running as root
            pytest.skip("permission bits are not enforced for this user")
        assert not storage.config_writable(target)
    finally:
        target.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_settings_save_before_any_model_and_apply_when_one_arrives(served) -> None:
    client, runtime, app = served
    res = client.put("/api/config/agents/lovia", json={"max_turns": 5})
    assert res.status_code == 200, res.text
    saved = json.loads(runtime.loaded.path.read_text())
    assert saved["version"] == 2
    assert saved["agents"][0]["max_turns"] == 5
    _add_model(client)
    assert app.state.deps.max_turns_for("lovia") == 5
