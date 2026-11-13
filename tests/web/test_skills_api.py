"""``GET /api/skills``: what the composer's ``/`` menu offers is exactly what
the chat's run sees — the same agent build, catalog, filter and precedence."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, Skills  # noqa: E402
from lovia.web import WorkspaceSwitch, create_app  # noqa: E402
from lovia.web.builder import project_agent, remote_agent  # noqa: E402
from lovia.web.config.schema import WebConfig  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.workspace import RemoteHost, Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402


def _skill(root: Path, name: str, description: str) -> None:
    (root / name).mkdir(parents=True)
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody", encoding="utf-8"
    )


def _listed(c: TestClient, **query: str) -> list[tuple[str, str]]:
    res = c.get("/api/skills", params=query)
    assert res.status_code == 200, res.text
    return [(s["name"], s["description"]) for s in res.json()]


def _index(system_prompt: str) -> list[tuple[str, str]]:
    """The skills a run's system prompt lists, in its order."""
    return re.findall(r"^- `([^`]+)` — (.*)$", system_prompt, flags=re.M)


@pytest.fixture
def setup(tmp_path: Path):
    """A served agent built the way ``lovia web`` builds it: the project's
    ``.agents/skills`` ahead of the global directory, by the agent's
    settings. ``review`` is in both — the project's wins."""
    shared, launch, proj = tmp_path / "shared", tmp_path / "launch", tmp_path / "proj"
    _skill(shared, "review", "global review")
    _skill(shared, "deploy", "ship it")
    _skill(launch / ".agents" / "skills", "launch-only", "the launch directory's")
    _skill(proj / ".agents" / "skills", "review", "project review")
    _skill(proj / ".agents" / "skills", "init", "write AGENTS.md")
    config = WebConfig.model_validate({"skills": {"dirs": [str(shared)]}})
    spec = config.default_agent
    provider = ScriptedProvider([text("ok")] * 4)
    agent = project_agent(
        Agent(name="bot", model=provider, workspace=Workspace.local(str(launch))),
        launch,
        config,
        spec,
    )
    return agent, provider, config, spec, launch, proj


def test_a_local_project_lists_what_its_run_sees(setup) -> None:
    agent, provider, config, spec, _launch, proj = setup
    switch = WorkspaceSwitch(
        agent_for=lambda a, root: project_agent(a, root, config, spec)
    )
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, workspaces=switch
    )
    want = [
        ("init", "write AGENTS.md"),
        ("review", "project review"),  # the project's shadows the global one
        ("deploy", "ship it"),
    ]
    with TestClient(app) as c:
        # A draft about to open in the project, then the chat once it exists.
        assert _listed(c, agent="bot", root=str(proj)) == want
        res = c.post(
            "/api/chat", json={"message": "/review now", "workspace": str(proj)}
        )
        assert res.status_code == 200, res.text
        assert _listed(c, session=res.json()["session_id"]) == want
        # The agent's own directory without a project: its own skills.
        assert ("launch-only", "the launch directory's") in _listed(c, agent="bot")
    (system, *_rest, last) = provider.calls[0]
    assert _index(str(system.content)) == want
    # The message reaches the model as typed: nothing expanded server-side.
    assert last.content == "/review now"


def test_a_custom_agents_skill_directories_apply(setup, tmp_path: Path) -> None:
    agent, _provider, config, _spec, _launch, proj = setup
    own = tmp_path / "own"
    _skill(own, "triage", "sort issues")
    spec = config.default_agent.model_copy(update={"skills": [str(own)]})
    switch = WorkspaceSwitch(
        agent_for=lambda a, root: project_agent(a, root, config, spec)
    )
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, workspaces=switch
    )
    with TestClient(app) as c:
        names = [n for n, _ in _listed(c, agent="bot", root=str(proj))]
    assert names == ["init", "review", "triage"]  # no global "deploy"


def test_a_filtered_out_skill_is_not_offered(tmp_path: Path) -> None:
    _skill(tmp_path, "public", "for everyone")
    _skill(tmp_path, "internal", "staff only")
    provider = ScriptedProvider([text("ok")])
    agent = Agent(
        name="bot",
        model=provider,
        plugins=[Skills(tmp_path, filter=lambda m: m.name != "internal")],
    )
    with TestClient(create_app(agent, store=ChatStore.in_memory(), titles=False)) as c:
        assert _listed(c) == [("public", "for everyone")]
        sid = c.post("/api/chat", json={"message": "hi"}).json()["session_id"]
        assert _listed(c, session=sid) == [("public", "for everyone")]
    assert _index(str(provider.calls[0][0].content)) == [("public", "for everyone")]


def test_an_agent_without_skills_lists_none() -> None:
    agent = Agent(name="bot", model=ScriptedProvider([]))
    with TestClient(create_app(agent, store=ChatStore.in_memory(), titles=False)) as c:
        assert _listed(c) == []
        res = c.get("/api/skills", params={"session": "nope"})
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "session_not_found"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX far side")
def test_a_remote_chat_lists_only_the_global_skills(setup, tmp_path: Path) -> None:
    # The project on the host has skills of its own, and the launch directory
    # here has some: a remote chat loads neither (#405), so neither is offered.
    agent, provider, config, spec, _launch, _proj = setup
    there = (tmp_path / "there").resolve()
    _skill(there / ".agents" / "skills", "remote-only", "on the host")
    far = RemoteHost("far", command=(), python=f"{sys.executable} -I -S")
    switch = WorkspaceSwitch(
        remotes={"far": far},
        agent_for=lambda a, root: project_agent(a, root, config, spec),
        agent_for_remote=lambda a, ref: remote_agent(a, config, spec),
    )
    app = create_app(
        agent, store=ChatStore.in_memory(), titles=False, workspaces=switch
    )
    key = f"remote://far{there}"
    want = [("deploy", "ship it"), ("review", "global review")]
    with TestClient(app) as c:
        try:
            assert _listed(c, agent="bot", root=key) == want
            res = c.post("/api/chat", json={"message": "go", "workspace": key})
            assert res.status_code == 200, res.text
            assert _listed(c, session=res.json()["session_id"]) == want
        finally:
            c.portal.call(far.aclose)
    assert _index(str(provider.calls[0][0].content)) == want
