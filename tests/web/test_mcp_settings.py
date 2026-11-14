"""Settings → MCP: the pasted JSON, masked secrets, the API, and a run that
starts without a server it could not reach."""

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
from lovia.web.config import (
    AgentConfig,
    ConfigRuntime,
    LoadedConfig,
    WebConfig,
    storage,
)  # noqa: E402
from lovia.web.config import runtime as runtime_module  # noqa: E402
from lovia.web.config.mcp import mask, parse_servers, restore_secrets, spec_of  # noqa: E402
from lovia.web.config.schema import MCPServerConfig  # noqa: E402
from lovia.web.mcp import SettingsMCP  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

STUB = str(Path(__file__).parents[1] / "plugins" / "mcp_stub_server.py")
TOKEN = "ghp_0123456789abcdef"


def _stub(name: str = "stub") -> dict[str, Any]:
    return {"command": sys.executable, "args": [STUB], "env": {"TOKEN": TOKEN}}


# ----------------------------------------------------------- the JSON in -


def test_takes_the_document_or_its_inside_in_the_shapes_clients_write() -> None:
    doc = {
        "mcpServers": {
            "fs": {"command": "npx", "args": ["-y", "server-fs", "/tmp"]},
            "web": {
                "type": "http",
                "url": "https://mcp.example/mcp",
                "headers": {"A": "b"},
            },
            "gem": {"httpUrl": "https://g.example/mcp"},
            "off": {
                "command": "uvx",
                "args": ["x"],
                "disabled": True,
                "env": {"PORT": 3000},
            },
        }
    }
    servers = {s.name: s for s in parse_servers(doc)}
    assert servers["fs"].transport == "stdio" and servers["fs"].args == [
        "-y",
        "server-fs",
        "/tmp",
    ]
    assert (servers["web"].transport, servers["web"].headers) == ("http", {"A": "b"})
    assert servers["gem"].url == "https://g.example/mcp"
    assert servers["off"].enabled is False and servers["off"].env == {"PORT": "3000"}
    assert [s.name for s in parse_servers(doc["mcpServers"])] == [
        "fs",
        "web",
        "gem",
        "off",
    ]


@pytest.mark.parametrize(
    ("doc", "says"),
    [
        ({"command": "npx"}, "name the server"),
        (
            {"x": {"type": "sse", "url": "https://a/sse"}},
            "SSE transport is not supported",
        ),
        ({"x": {"command": "a", "url": "https://a"}}, "either a command or a url"),
        ({"x": {}}, "either a command or a url"),
        ({"x": {"url": "ftp://a"}}, "http(s)"),
        ({"x": {"url": "https://a", "env": {"K": "v"}}}, "go with a command"),
        ({"x y": {"command": "a"}}, "letters, digits"),
        ({"x": {"command": "a", "args": "-y"}}, "list of strings"),
        ({}, "no servers"),
        ([], "expected an object"),
    ],
)
def test_says_what_is_wrong_with_a_document(doc: Any, says: str) -> None:
    with pytest.raises(ValueError, match=says.replace("(", r"\(").replace(")", r"\)")):
        parse_servers(doc)


def test_secrets_go_out_masked_and_come_back_as_they_were() -> None:
    stored = MCPServerConfig(
        name="gh", command="npx", env={"TOKEN": TOKEN, "SHORT": "abc", "GONE": "x" * 20}
    )
    out = spec_of(stored)
    assert out["env"] == {"TOKEN": mask(TOKEN), "SHORT": "…", "GONE": mask("x" * 20)}
    assert TOKEN not in json.dumps(out)
    # Untouched masks keep what is stored; "" removes; new text replaces.
    edited = parse_servers(
        {"gh": {**out, "env": {"TOKEN": mask(TOKEN), "SHORT": "new", "GONE": ""}}}
    )[0]
    assert restore_secrets(edited, stored).env == {"TOKEN": TOKEN, "SHORT": "new"}
    # A mask with nothing behind it — moved to another key, or a new server —
    # is refused, never saved as the placeholder.
    moved = parse_servers({"gh": {"command": "npx", "env": {"OTHER": mask(TOKEN)}}})[0]
    with pytest.raises(ValueError, match="env.OTHER is still a masked value"):
        restore_secrets(moved, stored)
    with pytest.raises(ValueError, match="masked"):
        restore_secrets(parse_servers({"gh": out})[0], None)


# ---------------------------------------------------------------- the API -


class RecordingProvider(ScriptedProvider):
    """Answers like ScriptedProvider and remembers the tools each call got."""

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
) -> Iterator[tuple[TestClient, ConfigRuntime, RecordingProvider]]:
    monkeypatch.chdir(tmp_path)
    provider = RecordingProvider()
    monkeypatch.setattr(
        runtime_module, "provider_from_string", lambda *a, **k: provider
    )
    path = storage.config_path(tmp_path / "home")
    config = WebConfig(
        agents=[AgentConfig(id="lovia", memory=False, subagents=False, followups=False)]
    )
    loaded = LoadedConfig(config, path, storage.display_path(path), exists=False)
    runtime = ConfigRuntime(
        deployment=Deployment(), loaded=loaded, store=ChatStore.in_memory()
    )
    app = create_app({}, store=runtime.store, config_runtime=runtime, titles=False)
    with TestClient(app, base_url="http://localhost") as client:
        res = client.post(
            "/api/config/models",
            json={"model": "m", "base_url": "https://gw.example/v1", "api_key": "k"},
        )
        assert res.status_code == 201, res.text
        yield client, runtime, provider


def _servers(client: TestClient) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in client.get("/api/config").json()["mcp"]["servers"]}


def test_add_edit_toggle_delete_keep_secrets_out_of_the_page(served, tmp_path) -> None:
    client, runtime, _ = served
    res = client.post(
        "/api/config/mcp", json={"document": {"mcpServers": {"stub": _stub()}}}
    )
    assert res.status_code == 201, res.text
    shown = _servers(client)["stub"]
    assert shown["spec"]["env"] == {"TOKEN": mask(TOKEN)} and shown["status"] is None
    assert TOKEN not in client.get("/api/config").text
    saved = json.loads((tmp_path / "home" / "config.json").read_text())
    assert saved["mcp"][0]["env"] == {"TOKEN": TOKEN}  # the real value, on disk only

    again = client.post("/api/config/mcp", json={"document": {"stub": _stub()}})
    assert (again.status_code, again.json()["detail"]["code"]) == (
        409,
        "mcp_server_exists",
    )

    # An edit sent back with the mask keeps the token; a rename moves it along.
    spec = {**shown["spec"], "args": [STUB, "--x"]}
    res = client.put(
        "/api/config/mcp/stub", json={"document": {"mcpServers": {"tools": spec}}}
    )
    assert res.status_code == 200, res.text
    (server,) = runtime.config.mcp
    assert (server.name, server.args, server.env) == (
        "tools",
        [STUB, "--x"],
        {"TOKEN": TOKEN},
    )

    res = client.put("/api/config/mcp/tools/enabled", json={"enabled": False})
    assert _servers(client)["tools"]["enabled"] is False
    assert client.delete("/api/config/mcp/tools").status_code == 200
    assert runtime.config.mcp == []
    missing = client.delete("/api/config/mcp/tools")
    assert (missing.status_code, missing.json()["detail"]["code"]) == (
        404,
        "mcp_server_not_found",
    )


def test_a_bad_document_is_refused_with_why(served) -> None:
    client, runtime, _ = served
    res = client.post(
        "/api/config/mcp", json={"document": {"x": {"type": "sse", "url": "https://a"}}}
    )
    assert res.status_code == 400 and "SSE" in res.json()["detail"]["message"]
    assert runtime.config.mcp == []


def test_writes_need_a_local_origin(served) -> None:
    client, _, _ = served
    res = client.post(
        "/api/config/mcp/test",
        json={"document": {"x": {"command": "echo"}}},
        headers={"host": "evil.example"},
    )
    assert (res.status_code, res.json()["detail"]["code"]) == (
        403,
        "local_origin_required",
    )


def test_testing_a_saved_server_records_its_last_connection(served) -> None:
    client, _, _ = served
    client.post(
        "/api/config/mcp",
        json={"document": {"stub": _stub(), "nope": {"command": "no-such-cmd-xyz"}}},
    )
    ok = client.post("/api/config/mcp/test", json={"name": "stub"}).json()["results"]
    assert ok[0]["ok"] is True and ok[0]["tools"] == ["add", "fail", "pic"]
    bad = client.post("/api/config/mcp/test", json={"name": "nope"}).json()["results"]
    assert bad[0]["ok"] is False and "command not found" in bad[0]["error"]
    servers = _servers(client)
    assert servers["stub"]["status"]["tools"] == ["add", "fail", "pic"]
    assert servers["nope"]["status"]["ok"] is False
    # An unsaved edit tests with the saved secrets behind its masks, and is
    # not anyone's last connection.
    spec = servers["stub"]["spec"]
    res = client.post(
        "/api/config/mcp/test", json={"document": {"stub": spec}, "original": "stub"}
    )
    assert res.json()["results"][0]["ok"] is True


def test_a_run_goes_on_without_a_server_it_cannot_reach(served) -> None:
    client, runtime, provider = served
    client.post(
        "/api/config/mcp",
        json={"document": {"stub": _stub(), "nope": {"command": "no-such-cmd-xyz"}}},
    )
    with client.stream("POST", "/api/chat/stream", json={"message": "hi"}) as res:
        frames = _sse("".join(res.iter_text()))
    names = [e for e, _ in frames]
    assert names[-1] == "done"
    (notice,) = [d for e, d in frames if e == "mcp_unavailable"]
    assert notice["servers"][0]["name"] == "nope"
    assert "command not found" in notice["servers"][0]["error"]
    # The reachable server's tools reached the model; the other's did not.
    assert {"stub__add", "stub__fail", "stub__pic"} <= set(provider.tools_seen[-1])
    assert not any(t.startswith("nope__") for t in provider.tools_seen[-1])
    servers = _servers(client)
    assert servers["stub"]["status"]["ok"] and servers["nope"]["status"]["ok"] is False


def test_an_agent_with_mcp_off_or_no_server_on_gets_no_plugin(served) -> None:
    client, runtime, _ = served
    client.post("/api/config/mcp", json={"document": {"stub": _stub()}})

    def mounted() -> bool:
        agents, _ = runtime.build()
        return any(isinstance(p, SettingsMCP) for p in agents["lovia"].plugins)

    assert mounted()
    client.put("/api/config/agents/lovia", json={"mcp": False})
    assert not mounted()
    client.put("/api/config/agents/lovia", json={"mcp": True})
    client.put("/api/config/mcp/stub/enabled", json={"enabled": False})
    assert not mounted()


def test_a_status_counts_for_the_server_as_it_was_tested(served) -> None:
    # An edit makes the last connection someone else's; on/off does not.
    client, runtime, _ = served
    client.post("/api/config/mcp", json={"document": {"stub": _stub()}})
    client.post("/api/config/mcp/test", json={"name": "stub"})
    assert _servers(client)["stub"]["status"]["ok"] is True
    client.put("/api/config/mcp/stub/enabled", json={"enabled": False})
    assert _servers(client)["stub"]["status"]["ok"] is True
    spec = {**_servers(client)["stub"]["spec"], "args": [STUB, "--edited"]}
    client.put("/api/config/mcp/stub", json={"document": {"stub": spec}})
    assert _servers(client)["stub"]["status"] is None
    # A run on the agents from before the edit reports late: not shown either.
    from lovia.web.mcp import MCPStatus

    old = MCPServerConfig(
        name="stub", command=sys.executable, args=[STUB], env={"TOKEN": TOKEN}
    )
    runtime.mcp.record(old, MCPStatus(ok=False, at=0.0, error="old"))
    assert _servers(client)["stub"]["status"] is None


def test_testing_needs_no_writable_config(served, monkeypatch) -> None:
    client, runtime, _ = served
    client.post("/api/config/mcp", json={"document": {"stub": _stub()}})
    monkeypatch.setattr(ConfigRuntime, "writable", property(lambda self: False))
    assert client.post("/api/config/mcp/test", json={"name": "stub"}).json()["results"][
        0
    ]["ok"]
    refused = client.put("/api/config/mcp/stub/enabled", json={"enabled": False})
    assert (refused.status_code, refused.json()["detail"]["code"]) == (
        409,
        "config_read_only",
    )


async def test_the_model_hears_which_servers_failed_but_not_their_words() -> None:
    from lovia.run_context import RunContext
    from lovia.web.mcp import MCPBook

    told: list[list[tuple[str, str]]] = []
    book = MCPBook(unavailable=lambda _ctx, failed: told.append(failed))
    servers = [
        MCPServerConfig(name="a", command="no-such-cmd-a"),
        MCPServerConfig(name="b", command="no-such-cmd-b"),
    ]
    inst = await SettingsMCP(servers, book).setup(
        RunContext(deps=None, entries=[], agent=None)
    )  # type: ignore[arg-type]
    assert inst.tools == []
    assert "a, b" in (inst.instructions or "") and "no-such-cmd" not in (
        inst.instructions or ""
    )
    # Said one by one, as each fails.
    assert [[n for n, _ in f] for f in told] == [["a"], ["b"]]


async def test_tools_ask_with_a_grant_unless_the_shell_runs_unasked() -> None:
    from types import SimpleNamespace

    from lovia.run_context import RunContext
    from lovia.web.mcp import MCPBook
    from lovia.workspace import WorkspacePolicy

    cfg = MCPServerConfig(name="stub", command=sys.executable, args=[STUB])
    inst = await SettingsMCP([cfg], MCPBook()).setup(
        RunContext(deps=None, entries=[], agent=None)  # type: ignore[arg-type]
    )
    try:
        add = next(t for t in inst.tools if t.name == "stub__add")

        def verdict(policy: WorkspacePolicy | None) -> tuple[str, tuple[str, ...]]:
            ws = None if policy is None else SimpleNamespace(policy=policy)
            ctx = RunContext(deps=None, entries=[], agent=None, workspace=ws)  # type: ignore[arg-type]
            v = add.decide({}, ctx)
            return v.action, v.scopes  # type: ignore[union-attr]

        assert verdict(WorkspacePolicy.trusted()) == ("allow", ())
        asked = ("ask", ("mcp:stub__add",))
        assert verdict(WorkspacePolicy.coding()) == asked
        assert verdict(WorkspacePolicy.readonly()) == asked
        assert verdict(None) == asked
    finally:
        assert inst.aclose is not None
        await inst.aclose()


def test_a_tool_whose_name_a_provider_refuses_is_left_out() -> None:
    from dataclasses import replace

    from lovia.plugins.mcp import MCPConnection
    from lovia.tools import tool
    from lovia.web.mcp import _usable

    @tool
    async def t() -> str:
        """x"""
        return ""

    conn = MCPConnection(transport=lambda: None, prefix="srv")
    conn._tools = [replace(t, name="srv__ok"), replace(t, name="srv__" + "x" * 60)]
    assert [x.name for x in _usable(conn)] == ["srv__ok"]


async def test_a_server_that_never_answers_is_given_up_on(tmp_path) -> None:
    # Within the run's own task (asyncio.timeout, or the 3.10 stand-in), so
    # the transport is closed by the task that opened it, and nothing stays.
    import asyncio

    from lovia.web.mcp import probe

    hang = tmp_path / "hang.py"
    hang.write_text("import sys, time\nsys.stdin.readline()\ntime.sleep(60)\n")
    cfg = MCPServerConfig(name="hang", command=sys.executable, args=[str(hang)])
    status = await probe(cfg, timeout=0.5)
    assert (status.ok, status.error) == (False, "no answer within 0.5 s")
    await asyncio.sleep(0)
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


def _sse(raw: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    event, data = "message", ""
    for line in [*raw.splitlines(), ""]:
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()
        elif not line and data:
            out.append((event, json.loads(data)))
            event, data = "message", ""
    return out
