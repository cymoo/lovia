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


def test_a_commands_directory_is_the_projects_or_relative_to_it(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "here": {"command": "x"},
            "sub": {"command": "x", "cwd": "tools"},
            "abs": {"command": "x", "cwd": "/opt/mcp"},
            "off": {"command": "x", "disabled": True},
        },
    )
    by_name = {s.name: s for s in read_project_servers(tmp_path, {})[0]}
    assert by_name["here"].config.cwd == str(tmp_path)
    assert by_name["sub"].config.cwd == str(tmp_path / "tools")
    assert by_name["abs"].config.cwd == "/opt/mcp"
    # Turned off in the file: listed, with why it can't be turned on here.
    assert by_name["off"].config is None and "disabled" in by_name["off"].error


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


def test_a_project_has_global_servers_on_or_off_for_itself(served) -> None:
    client, provider, project = served
    res = client.post(
        "/api/config/mcp",
        json={"document": {"always": _stub(), "rarely": {**_stub(), "disabled": True}}},
    )
    assert res.status_code == 201, res.text

    def as_here() -> dict[str, tuple[bool, bool]]:
        return {
            s["name"]: (s["enabled"], s["on"]) for s in client.get(URL).json()["global"]
        }

    # Each follows its own switch until this project says otherwise.
    assert as_here() == {"always": (True, True), "rarely": (False, False)}
    assert {t.split("__")[0] for t in _chat(client, provider)} == {"always"}

    for name, on in (("always", False), ("rarely", True)):
        res = client.put(URL, json={"name": name, "on": on, "source": "global"})
        assert res.status_code == 200, res.text
    assert as_here() == {"always": (True, False), "rarely": (False, True)}
    assert {t.split("__")[0] for t in _chat(client, provider)} == {"rarely"}
    # Settings itself is untouched: every other project sees what it did.
    config = client.get("/api/config").json()["mcp"]["servers"]
    assert {s["name"]: s["enabled"] for s in config} == {
        "always": True,
        "rarely": False,
    }

    # Back on its own switch: nothing is remembered, so it follows it again.
    client.put(URL, json={"name": "always", "on": True, "source": "global"})
    client.put("/api/config/mcp/always/enabled", json={"enabled": False})
    assert as_here()["always"] == (False, False)

    res = client.put(URL, json={"name": "nope", "on": True, "source": "global"})
    assert res.status_code == 404


def test_a_server_added_for_one_project_is_on_there_alone(served) -> None:
    client, provider, project = served
    res = client.post(
        "/api/config/mcp", json={"document": {"mine": _stub()}, "root": str(project)}
    )
    assert res.status_code == 201, res.text
    # Saved like any other — off for every chat, and says who still uses it.
    (saved,) = res.json()["mcp"]["servers"]
    assert saved["enabled"] is False
    assert saved["projects"] == [str(project.resolve())]
    # On in the project it was added to: its chats get the tools.
    (here,) = client.get(URL).json()["global"]
    assert (here["enabled"], here["on"]) == (False, True)
    assert {t.split("__")[0] for t in _chat(client, provider)} == {"mine"}
    # Turned on for everyone, it no longer belongs to some projects only.
    client.put("/api/config/mcp/mine/enabled", json={"enabled": True})
    (saved,) = client.get("/api/config").json()["mcp"]["servers"]
    assert saved["projects"] == []
    # One the pasted document itself disables is saved, and stays off here.
    doc = {"later": {**_stub(), "disabled": True}}
    res = client.post("/api/config/mcp", json={"document": doc, "root": str(project)})
    assert res.status_code == 201, res.text
    assert {s["name"]: s["on"] for s in client.get(URL).json()["global"]} == {
        "mine": True,
        "later": False,
    }
    # Nothing is saved for a name that is taken.
    res = client.post(
        "/api/config/mcp", json={"document": {"mine": _stub()}, "root": str(project)}
    )
    assert res.status_code == 409


def test_what_a_project_said_of_a_server_goes_with_the_server(served) -> None:
    client, _provider, _project = served
    doc = {"db": {**_stub(), "disabled": True}}
    assert client.post("/api/config/mcp", json={"document": doc}).status_code == 201
    client.put(URL, json={"name": "db", "on": True, "source": "global"})

    def on_here() -> dict[str, bool]:
        return {s["name"]: s["on"] for s in client.get(URL).json()["global"]}

    # Renamed: it is the same server, on in this project as before.
    renamed = {"store": {**_stub(), "disabled": True}}
    res = client.put("/api/config/mcp/db", json={"document": renamed})
    assert res.status_code == 200, res.text
    assert on_here() == {"store": True}
    # Deleted, and another server takes the name later: it starts as that
    # server's own switch says, not as the old one was left here.
    assert client.delete("/api/config/mcp/store").status_code == 200
    again = {"store": {**_stub(), "disabled": True}}
    assert client.post("/api/config/mcp", json={"document": again}).status_code == 201
    assert on_here() == {"store": False}


def test_a_global_server_is_replaced_only_by_one_a_run_starts(served) -> None:
    client, _provider, project = served
    assert (
        client.post("/api/config/mcp", json={"document": {"db": _stub()}}).status_code
        == 201
    )

    def replaced() -> bool:
        (server,) = client.get(URL).json()["global"]
        return server["replaced"]

    _write(project, {"db": _stub()})
    assert not replaced()  # undecided: the global one still runs
    client.put(URL, json={"name": "db", "on": True})
    assert replaced()
    # Turned on, but no longer usable as written: runs skip it, and so the
    # global one is what they start.
    _write(project, {"db": {**_stub(), "env": {"K": "${NOT_SET_ANYWHERE_XYZ}"}}})
    client.put(URL, json={"name": "db", "on": True})
    assert not replaced()


def test_the_record_keeps_both_kinds_of_decision_apart(tmp_path: Path) -> None:
    project, book = tmp_path / "p", ProjectMCP(tmp_path / "home")
    project.mkdir()
    key = str(project)
    _write(project, {"db": {"command": "x"}})
    (server,) = read_project_servers(project)[0]
    book.decide(key, server, True)
    # A global server named like the project's own: its own entry.
    book.override(key, "db", False)
    assert book.states(key, [server]) == {"db": "on"}
    assert book.overrides(key) == {"db": False}
    book.override(key, "db", None)
    assert book.overrides(key) == {}
    assert book.states(key, [server]) == {"db": "on"}
    saved = json.loads((tmp_path / "home" / "mcp-projects.json").read_text())
    assert saved["global"] == {} and list(saved["projects"]) == [key]


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


def test_a_read_only_home_refuses_the_decision(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _provider, project = served
    _write(project, {"proj": _stub()})
    monkeypatch.setattr(runtime_module, "config_writable", lambda path: False)
    res = client.put(URL, json={"name": "proj", "on": True})
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "config_read_only"


def test_turning_one_on_needs_a_local_origin(served) -> None:
    client, _provider, project = served
    _write(project, {"proj": _stub()})
    res = client.put(
        URL, json={"name": "proj", "on": True}, headers={"host": "evil.example"}
    )
    assert res.status_code == 403
    assert client.get(URL).json()["servers"][0]["state"] == "pending"
