"""A project's own MCP servers: ``.mcp.json``, read per run, each server off
until someone turned it on for the entry as written."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from fastapi.testclient import TestClient  # noqa: E402

from lovia.testing import ScriptedProvider, text  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.builder import Deployment  # noqa: E402
from lovia.web.config import (  # noqa: E402
    AssistantConfig,
    ConfigRuntime,
    LoadedConfig,
    WebConfig,
    storage,
)
from lovia.web.config import runtime as runtime_module  # noqa: E402
from lovia.web.project_mcp import ProjectMCP, read_project_servers  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

STUB = str(Path(__file__).parents[1] / "plugins" / "mcp_stub_server.py")
URL = "/api/config/project/mcp"


def _write(root: Path, servers: dict[str, Any]) -> None:
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def _stub() -> dict[str, Any]:
    return {"command": sys.executable, "args": [STUB]}


# ----------------------------------------------------------------- the file -


def test_no_file_is_no_servers_and_no_complaint(tmp_path: Path) -> None:
    assert read_project_servers(tmp_path) == ([], None)


def test_entries_read_like_any_clients_with_variables_filled(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "db": {
                "command": "npx",
                "args": ["-y", "db-mcp", "--dsn", "${DSN}"],
                "env": {"REGION": "${REGION:-eu}"},
            },
            "docs": {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"},
            },
        },
    )
    env = {"DSN": "postgres://x", "DOCS_TOKEN": "t0k"}
    (db, docs), error = read_project_servers(tmp_path, env)
    assert error is None
    assert db.config is not None and docs.config is not None
    assert db.config.args == ["-y", "db-mcp", "--dsn", "postgres://x"]
    assert db.config.env == {"REGION": "eu"}
    # A command with no directory of its own starts in the project.
    assert db.config.cwd == str(tmp_path)
    assert docs.config.headers == {"Authorization": "Bearer t0k"}
    # What is shown and approved is the entry as written, not as filled.
    assert db.spec["args"][-1] == "${DSN}"
    same, _ = read_project_servers(tmp_path, {**env, "DSN": "postgres://y"})
    assert same[0].digest == db.digest


def test_one_unusable_entry_does_not_take_the_others_down(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "needs": {"command": "x", "env": {"KEY": "${NOT_SET_ANYWHERE}"}},
            "old": {"type": "sse", "url": "https://example.com/sse"},
            "fine": {"command": "x"},
        },
    )
    servers, error = read_project_servers(tmp_path, {})
    assert error is None
    by_name = {s.name: s for s in servers}
    assert by_name["needs"].error == "the variable NOT_SET_ANYWHERE is not set"
    assert "SSE" in (by_name["old"].error or "")
    assert by_name["fine"].config is not None and by_name["fine"].error is None


def test_a_file_that_is_not_the_projects_is_not_read(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    servers, error = read_project_servers(tmp_path)
    assert servers == [] and error is not None and ".mcp.json" in error
    (tmp_path / ".mcp.json").write_text("[]", encoding="utf-8")
    assert "mcpServers" in (read_project_servers(tmp_path)[1] or "")
    if sys.platform != "win32":
        outside, project = tmp_path / "outside", tmp_path / "project"
        outside.mkdir()
        project.mkdir()
        _write(outside, {"x": {"command": "x"}})
        (project / ".mcp.json").symlink_to(outside / ".mcp.json")
        assert read_project_servers(project) == ([], None)


# ------------------------------------------------------------- the decision -


def test_a_decision_holds_for_the_entry_it_was_made_on(tmp_path: Path) -> None:
    project, book = tmp_path / "p", ProjectMCP(tmp_path / "home")
    project.mkdir()
    key = str(project)
    _write(project, {"a": {"command": "x"}, "b": {"command": "y"}})
    servers, _ = read_project_servers(project)
    assert book.states(key, servers) == {"a": "pending", "b": "pending"}
    assert book.approved(key) == []
    book.decide(key, servers[0], True)
    book.decide(key, servers[1], False)
    assert book.states(key, servers) == {"a": "on", "b": "off"}
    assert [s.name for s in book.approved(key)] == ["a"]
    # Another project naming the same server decides for itself.
    assert book.approved(str(tmp_path / "elsewhere")) == []
    # The entry changes — a pull, or the agent's own edit: undecided again.
    _write(project, {"a": {"command": "x", "args": ["--rm"]}, "b": {"command": "y"}})
    servers, _ = read_project_servers(project)
    assert book.states(key, servers) == {"a": "pending", "b": "off"}
    assert book.approved(key) == []


def test_an_unreadable_record_turns_nothing_on(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "mcp-projects.json").write_text("{broken", encoding="utf-8")
    _write(tmp_path, {"a": {"command": "x"}})
    assert ProjectMCP(home).approved(str(tmp_path)) == []


# ------------------------------------------------------------ over the API -


class RecordingProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([text("ok") for _ in range(20)])
        self.tools_seen: list[list[str]] = []

    def stream(self, entries: Any, *, tools: Any = None, **kw: Any) -> Any:
        self.tools_seen.append(
            [
                t["function"]["name"] if "function" in t else t["name"]
                for t in tools or []
            ]
        )
        return super().stream(entries, tools=tools, **kw)


@pytest.fixture
def served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[TestClient, RecordingProvider, Path]]:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    provider = RecordingProvider()
    monkeypatch.setattr(
        runtime_module, "provider_from_string", lambda *a, **k: provider
    )
    home = tmp_path / "home"
    path = storage.config_path(home)
    config = WebConfig(
        assistant=AssistantConfig(memory=False, subagents=False, followups=False)
    )
    loaded = LoadedConfig(config, path, storage.display_path(path), exists=False)
    runtime = ConfigRuntime(
        deployment=Deployment(workspace=str(project), home=home),
        loaded=loaded,
        store=ChatStore.in_memory(),
    )
    app = create_app({}, store=runtime.store, config_runtime=runtime, titles=False)
    with TestClient(app, base_url="http://localhost") as client:
        res = client.post(
            "/api/config/models",
            json={"model": "m", "base_url": "https://gw.example/v1", "api_key": "k"},
        )
        assert res.status_code == 201, res.text
        yield client, provider, project


def _chat(client: TestClient, provider: RecordingProvider) -> set[str]:
    """Run one message; the MCP tools that run's model was offered."""
    res = client.post("/api/chat", json={"message": "hi"})
    assert res.status_code == 200, res.text
    return {t for t in provider.tools_seen[-1] if "__" in t}


def test_a_projects_server_runs_only_once_turned_on(served) -> None:
    client, provider, project = served
    _write(project, {"proj": _stub()})
    listing = client.get(URL).json()
    assert listing["project"] == str(project.resolve())
    (server,) = listing["servers"]
    assert (server["name"], server["state"]) == ("proj", "pending")
    assert server["spec"] == _stub()
    # Undecided: the run goes on without it.
    assert _chat(client, provider) == set()

    res = client.put(URL, json={"name": "proj", "on": True})
    assert res.status_code == 200, res.text
    assert res.json()["servers"][0]["state"] == "on"
    assert {"proj__add", "proj__fail", "proj__pic"} <= _chat(client, provider)
    # Its last connection is on the listing, like a global server's.
    assert client.get(URL).json()["servers"][0]["status"]["ok"] is True

    # The entry changed under it: off again until looked at.
    _write(project, {"proj": {**_stub(), "env": {"MODE": "x"}}})
    assert client.get(URL).json()["servers"][0]["state"] == "pending"
    assert _chat(client, provider) == set()

    client.put(URL, json={"name": "proj", "on": False})
    assert client.get(URL).json()["servers"][0]["state"] == "off"
    assert _chat(client, provider) == set()


def test_the_chats_own_project_is_the_one_asked_about(served) -> None:
    client, provider, project = served
    _write(project, {"proj": _stub()})
    sid = client.post("/api/chat", json={"message": "hi"}).json()["session_id"]
    assert client.get(URL, params={"session": sid}).json()["servers"]
    res = client.put(URL, json={"session": sid, "name": "proj", "on": True})
    assert res.status_code == 200, res.text
    assert client.get(URL, params={"session": "nope"}).status_code == 404
    res = client.put(URL, json={"name": "absent", "on": True})
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "mcp_server_not_found"


def test_a_projects_server_stands_in_for_a_global_one_of_its_name(served) -> None:
    client, provider, project = served
    res = client.post(
        "/api/config/mcp",
        json={"document": {"stub": {"command": "no-such-cmd-xyz"}, "other": _stub()}},
    )
    assert res.status_code == 201, res.text
    _write(project, {"stub": _stub()})
    client.put(URL, json={"name": "stub", "on": True})
    tools = _chat(client, provider)
    # The project's "stub" answered; the global one of that name wasn't tried.
    assert {"stub__add", "other__add"} <= tools


def test_turning_one_on_needs_a_local_origin(served) -> None:
    client, _provider, project = served
    _write(project, {"proj": _stub()})
    res = client.put(
        URL, json={"name": "proj", "on": True}, headers={"host": "evil.example"}
    )
    assert res.status_code == 403
    assert client.get(URL).json()["servers"][0]["state"] == "pending"
