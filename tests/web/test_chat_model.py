"""A chat's own model: picked for a new chat or switched later, it is what
the chat's next run is on; a chat that picked none follows the default."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent, ImagePart  # noqa: E402
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
from lovia.web.store import ChatStore  # noqa: E402


class Model(ScriptedProvider):
    """A provider that counts the runs it served."""

    def __init__(self, model: str, *, vision: bool = False) -> None:
        super().__init__([text("ok") for _ in range(20)])
        self.model = model
        self.supports_vision = vision
        self.runs = 0
        self.images = 0  # image parts in the last prompt it was sent

    def stream(self, entries: Any, **kw: Any) -> Any:
        self.runs += 1
        self.images = sum(
            isinstance(part, ImagePart)
            for entry in entries
            if isinstance(getattr(entry, "content", None), list)
            for part in entry.content
        )
        return super().stream(entries, **kw)


@pytest.fixture
def served(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[tuple[TestClient, ConfigRuntime, dict[str, list[Model]]]]:
    """The default app's runtime on two models, ``main`` (the default) and
    ``fast``; ``built`` lists every provider made for each model name."""
    monkeypatch.chdir(tmp_path)
    built: dict[str, list[Model]] = {}

    def make(model: str, **kw: Any) -> Model:
        if model.startswith("bad"):
            raise ValueError("no such vendor")
        provider = Model(model, vision=bool(kw.get("supports_vision")))
        built.setdefault(model, []).append(provider)
        return provider

    monkeypatch.setattr(runtime_module, "provider_from_string", make)
    path = storage.config_path(tmp_path / "home")
    config = WebConfig(
        assistant=AssistantConfig(memory=False, subagents=False, followups=False)
    )
    loaded = LoadedConfig(config, path, storage.display_path(path), exists=False)
    runtime = ConfigRuntime(
        deployment=Deployment(workspace=str(tmp_path), home=tmp_path / "home"),
        loaded=loaded,
        store=ChatStore.in_memory(),
    )
    app = create_app({}, store=runtime.store, config_runtime=runtime, titles=False)
    with TestClient(app, base_url="http://localhost") as client:
        for name, model, extra in (
            ("main", "main-model", {}),
            ("fast", "fast-model", {"max_tokens": 512, "context_window": 32_000}),
        ):
            res = client.post(
                "/api/config/models",
                json={
                    "name": name,
                    "model": model,
                    "base_url": "https://gw.example/v1",
                    "api_key": "k",
                    **extra,
                },
            )
            assert res.status_code == 201, res.text
        yield client, runtime, built


def _runs(built: dict[str, list[Model]]) -> dict[str, int]:
    return {name: sum(p.runs for p in made) for name, made in built.items()}


def _send(client: TestClient, **body: Any) -> str:
    res = client.post("/api/chat", json={"message": "hi", **body})
    assert res.status_code == 200, res.text
    return res.json()["session_id"]


def test_a_chat_that_picks_none_runs_on_the_default(served) -> None:
    client, _runtime, built = served
    assert client.get("/api/info").json()["features"]["chat_model"] is True
    sid = _send(client)
    assert _runs(built) == {"main-model": 1}
    assert client.get(f"/api/sessions/{sid}").json()["model"] is None


def test_a_new_chat_runs_on_the_model_it_picked_and_stays_on_it(served) -> None:
    client, _runtime, built = served
    sid = _send(client, model="fast")
    assert _runs(built)["fast-model"] == 1
    assert client.get(f"/api/sessions/{sid}").json()["model"] == "fast"
    (row,) = client.get("/api/sessions").json()
    assert row["model"] == "fast"
    # The next message names no model — and one it names is not the chat's
    # to change that way: it stays where it is.
    _send(client, session_id=sid)
    _send(client, session_id=sid, model="main")
    assert _runs(built) == {"main-model": 0, "fast-model": 3}
    # One provider for the model, however many runs.
    assert len(built["fast-model"]) == 1


def test_a_chat_switches_model_between_runs(served) -> None:
    client, _runtime, built = served
    sid = _send(client)
    res = client.patch(f"/api/sessions/{sid}", json={"model": "fast"})
    assert res.status_code == 200, res.text
    assert res.json()["model"] == "fast"
    _send(client, session_id=sid)
    # Back on the default: an explicit null, which a rename doesn't send.
    client.patch(f"/api/sessions/{sid}", json={"title": "renamed"})
    assert client.get(f"/api/sessions/{sid}").json()["model"] == "fast"
    client.patch(f"/api/sessions/{sid}", json={"model": None})
    _send(client, session_id=sid)
    assert _runs(built) == {"main-model": 2, "fast-model": 1}


def test_a_model_this_server_does_not_have_is_refused(served) -> None:
    client, _runtime, built = served
    res = client.post("/api/chat", json={"message": "hi", "model": "nope"})
    assert res.status_code == 400
    assert "main" in res.json()["detail"]["hint"]
    assert client.get("/api/sessions").json() == []  # nothing was started
    sid = _send(client)
    res = client.patch(f"/api/sessions/{sid}", json={"model": "nope"})
    assert res.status_code == 400
    assert client.get(f"/api/sessions/{sid}").json()["model"] is None


def test_the_agent_is_sized_to_the_chats_model(served) -> None:
    client, runtime, _built = served
    agent = client.app.state.deps.agents["lovia"]
    fast = runtime.on_model(agent, "fast")
    assert fast.model.model == "fast-model"
    assert fast.settings.max_tokens == 512
    assert fast.context_policy.context_window == 32_000
    assert fast.name == agent.name and fast.plugins == agent.plugins
    # The default's own id, or one that is gone: the agent as it is.
    assert runtime.on_model(agent, "main") is agent
    assert runtime.on_model(agent, "deleted-since") is agent


def test_a_pick_follows_the_models_settings_and_outlives_its_deletion(served) -> None:
    client, _runtime, built = served
    sid = _send(client, model="fast")
    # Edited in Settings: the chat's next run is on the model as it is now.
    res = client.put(
        "/api/config/models/fast",
        json={"model": "faster-model", "base_url": "https://gw.example/v1"},
    )
    assert res.status_code == 200, res.text
    _send(client, session_id=sid)
    assert _runs(built)["faster-model"] == 1
    # Deleted: the chat carries on, on the default.
    assert client.delete("/api/config/models/fast").status_code == 200
    before = _runs(built)["main-model"]
    _send(client, session_id=sid)
    assert _runs(built)["main-model"] == before + 1


def test_an_attached_image_goes_as_the_chats_model_can_take_it(
    served, tmp_path: Path
) -> None:
    client, _runtime, built = served
    res = client.post(
        "/api/config/models",
        json={
            "name": "eyes",
            "model": "eyes-model",
            "base_url": "https://gw.example/v1",
            "api_key": "k",
            "vision": "on",
        },
    )
    assert res.status_code == 201, res.text
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads/cat.png").write_bytes(
        bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32
    )
    image = {"path": "uploads/cat.png", "mime": "image/png", "kind": "image"}
    # The default can't see: a path note. The chat's own model can: pixels.
    _send(client, attachments=[image])
    assert built["main-model"][-1].images == 0
    _send(client, model="eyes", attachments=[image])
    assert built["eyes-model"][-1].images == 1


def test_a_pick_that_cannot_be_run_on_is_refused(served) -> None:
    client, _runtime, _built = served
    sid = _send(client, model="fast")
    res = client.put(
        "/api/config/models/fast",
        json={"model": "bad-model", "base_url": "https://gw.example/v1"},
    )
    assert res.status_code == 200, res.text
    res = client.post("/api/chat", json={"message": "hi", "session_id": sid})
    assert res.status_code == 400
    assert "no such vendor" in res.json()["detail"]["message"]


def test_a_customized_context_policy_stays_on_another_model(served) -> None:
    client, runtime, _built = served

    class Policy:
        name = "mine"

    agent = client.app.state.deps.agents["lovia"].clone(context_policy=Policy())
    assert isinstance(runtime.on_model(agent, "fast").context_policy, Policy)


def test_the_vision_tool_follows_the_model() -> None:
    from lovia import tool
    from lovia.web.builder import on_model
    from lovia.web.config import ModelProfile
    from lovia.web.vision import DESCRIBE_IMAGE

    @tool(name=DESCRIBE_IMAGE)
    def old(path: str) -> str:
        """Describe an image."""
        return path

    @tool(name=DESCRIBE_IMAGE)
    def new(path: str) -> str:
        """Describe an image."""
        return path

    @tool
    def other() -> str:
        """Something else."""
        return ""

    agent = Agent(name="bot", model=Model("a"), tools=[old, other])
    profile = ModelProfile(id="b", model="b")
    # A model that sees for itself drops the tool; one that can't gets its own.
    assert on_model(agent, Model("b"), profile, None).tools == [other]
    assert on_model(agent, Model("b"), profile, new).tools == [other, new]


def test_settings_says_what_window_a_model_has(served) -> None:
    client, _runtime, _built = served
    models = {m["id"]: m for m in client.get("/api/config").json()["models"]}
    assert models["fast"]["window"] == 32_000


async def test_a_fork_carries_the_model_on(tmp_path: Path) -> None:
    from lovia.transcript import InputEntry

    store = ChatStore.sqlite(tmp_path / "chats.db")
    await store.upsert("src", agent="lovia", model="fast")
    entry = InputEntry(role="user", content="hi")
    await store.session.append("src", [entry], run_id="r1")
    fork = await store.fork("src")
    assert fork is not None
    meta = await store.get(fork)
    assert meta is not None and meta.model == "fast"
    # A touch never changes it; set_model does.
    await store.upsert("src", agent="lovia", model="other")
    assert (await store.get("src")).model == "fast"  # type: ignore[union-attr]
    await store.set_model("src", None)
    assert (await store.get("src")).model is None  # type: ignore[union-attr]


def test_a_server_with_no_models_to_pick_from_says_so(tmp_path: Path) -> None:
    agent = Agent(name="bot", model=ScriptedProvider([text("ok")]))
    app = create_app(agent, store=ChatStore.in_memory(), titles=False)
    with TestClient(app) as client:
        assert client.get("/api/info").json()["features"]["chat_model"] is False
        res = client.post("/api/chat", json={"message": "hi", "model": "fast"})
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "feature_unavailable"
