"""``default_app``: the app ``lovia web`` serves, built from code."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, Subagents, tool  # noqa: E402
from lovia.exceptions import UserError  # noqa: E402
from lovia.web import default_app  # noqa: E402
from lovia.web import __main__ as cli  # noqa: E402


@tool
def lookup(order: str) -> str:
    """Look an order up."""
    return order


def _client(app: Any) -> TestClient:
    return TestClient(app, base_url="http://localhost")


def test_a_fresh_home_serves_the_setup_screen(tmp_path: Path) -> None:
    home = tmp_path / "home"
    app = default_app(home=home, workspace=tmp_path)
    with _client(app) as c:
        info = c.get("/api/info").json()
        assert info["configured"] is False and info["agents"] == []
        assert info["features"]["settings"] is True
        # Settings, the chat history and memory all live in the home.
        assert c.get("/api/config").json()["scope"]["label"].endswith("config.json")
    assert (home / "lovia.db").is_file()
    assert "*" in (home / ".gitignore").read_text().splitlines()


def test_it_is_the_app_lovia_web_serves(
    tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    home = tmp_path / "home"
    write_config(home=home)
    with _client(default_app(home=home, workspace=tmp_path)) as c:
        info = c.get("/api/info").json()
        assert info["agents"] == ["lovia"] and info["configured"] is True
        # Every permission mode, follow-ups on: what `lovia web` gives.
        assert info["workspace_modes"]["modes"] == ["readonly", "coding", "trusted"]
        assert info["features"]["followups"] is True
        assert c.app.state.deps.max_turns_for("lovia") == 150


def test_the_switches_are_the_command_lines(
    tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    home = tmp_path / "home"
    write_config(home=home)
    with _client(default_app(home=home, workspace=tmp_path, readonly=True)) as c:
        assert c.get("/api/info").json()["workspace_modes"]["modes"] == ["readonly"]
    with _client(default_app(home=home, workspace=None)) as c:
        info = c.get("/api/info").json()
        assert info["workspace_modes"] is None
        assert info["features"]["workspace"] is False
    locked = default_app(home=home, workspace=tmp_path, lock_workspace=True)
    with _client(locked) as c:
        assert c.get("/api/workspaces").json()["can_add"] is False
    elsewhere = tmp_path / "chats.db"
    default_app(home=home, workspace=tmp_path, db_path=elsewhere)
    assert elsewhere.is_file()


def test_relative_paths_are_settled_when_the_app_is_built(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "ws").mkdir()
    monkeypatch.chdir(tmp_path)
    app = default_app(home="home", workspace="ws")
    deployment = app.state.deps.config_runtime.deployment
    # The agent is rebuilt from these on every save: they must not follow
    # the working directory around.
    assert deployment.home == tmp_path / "home"
    assert deployment.workspace == str(tmp_path / "ws")


def test_customize_has_the_last_word_on_the_assistant_and_its_child(
    tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    home = tmp_path / "home"
    write_config(home=home)
    seen: list[str] = []

    def customize(agent: Agent[Any]) -> Agent[Any]:
        seen.append(agent.name)
        return agent.clone(tools=[*agent.tools, lookup])

    app = default_app(home=home, workspace=tmp_path, customize=customize)
    with _client(app) as c:
        # The child first: it goes into the assistant's Subagents plugin.
        assert seen == ["lovia.sub", "lovia"]
        agent = app.state.deps.agents["lovia"]
        (subagents,) = [p for p in agent.plugins if isinstance(p, Subagents)]
        for built in (agent, *subagents.agents):
            assert "lookup" in {t.name for t in built.tools}
        assert "lookup" in c.get("/api/agents/lovia").json()["tools"]
        # A save in Settings rebuilds the agent: customized again.
        res = c.put("/api/config/assistant", json={"search": False})
        assert res.status_code == 200, res.text
        rebuilt = app.state.deps.agents["lovia"]
        assert rebuilt is not agent
        tools = {t.name for t in rebuilt.tools}
        assert "lookup" in tools and "web_search" not in tools
        # A project chat keeps it too: only the instructions are per project.
        (tmp_path / "AGENTS.md").write_text("PROJECT RULES", encoding="utf-8")
        moved = app.state.deps.config_runtime.agent_for(rebuilt, tmp_path)
        assert "PROJECT RULES" in moved.instructions
        assert "lookup" in {t.name for t in moved.tools}


def test_customize_must_return_the_agent_it_was_given(
    tmp_path: Path, write_config: Callable[..., Path]
) -> None:
    home = tmp_path / "home"
    write_config(home=home)
    with pytest.raises(UserError, match="customize must return"):
        default_app(
            home=home, workspace=tmp_path, customize=lambda a: a.clone(name="mine")
        )
    with pytest.raises(UserError, match="customize must return"):
        default_app(home=home, workspace=tmp_path, customize=lambda a: None)  # type: ignore[arg-type,return-value]


def test_a_home_that_is_a_repository_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with pytest.raises(UserError, match="Git repository"):
        default_app(home=tmp_path, workspace=tmp_path)


def test_lovia_web_can_serve_a_customized_default_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_config: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    write_config(home=home)
    (tmp_path / "mine.py").write_text(
        "from lovia.web import default_app\n"
        f"app = default_app(home={str(home)!r}, workspace={str(tmp_path)!r})\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    served: dict[str, Any] = {}
    monkeypatch.setattr(cli, "serve", lambda app, **kw: served.update(app=app, **kw))
    assert cli.main(["--app", "mine:app", "--port", "9001"]) == 0
    assert served["port"] == 9001
    assert list(served["app"].state.deps.agents) == ["lovia"]
