"""Tests for Memory's scopes: a user layer everywhere, one layer per scope key.

Network-free like ``test_memory.py``: the scripted provider drives runs.
``scope=None`` behavior is covered by ``test_memory.py`` itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lovia import Agent, InMemorySession, Runner, tool
from lovia.exceptions import UserError
from lovia.plugins.memory.index import KeywordIndex
from lovia.plugins.memory.plugin import (
    FileNotesStore,
    Memory,
    _best_match,
    _scope_dirname,
    _strip_date,
)
from lovia.workspace import Workspace

from ..scripted_provider import ScriptedProvider, call, text
from .test_memory import _msgs
from .test_memory_vector import FakeEmbedder


def _bare(facts: list[str]) -> list[str]:
    return [_strip_date(f) for f in facts]


def _scoped(root: Path, **kwargs: Any) -> Memory:
    """A Memory whose scope key is the run's deps (``None`` = no scope)."""
    return Memory(root, scope=lambda ctx: ctx.deps, **kwargs)


def _ctx(key: str | None, session_id: str | None = "s1", run_id: str = "r1") -> Any:
    return SimpleNamespace(
        deps=key,
        run_id=run_id,
        session_id=session_id,
        agent=SimpleNamespace(model="test-model"),
    )


def _scope_dir(root: Path, key: str) -> Path:
    return root / "scopes" / _scope_dirname(key)


async def _notes(mem: Memory, scope: str | None = None) -> list[str]:
    return _bare(await mem._tier(scope).notes.load())


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_scope_rejects_custom_stores(tmp_path) -> None:
    with pytest.raises(UserError, match="notes="):
        Memory(
            tmp_path,
            scope=lambda ctx: "k",
            notes=FileNotesStore(tmp_path / "n.md"),
        )
    with pytest.raises(UserError, match="index="):
        Memory(tmp_path, scope=lambda ctx: "k", index=KeywordIndex(":memory:"))
    # index=None names no store — it only turns recall off, in every layer.
    Memory(tmp_path, scope=lambda ctx: "k", index=None)


@pytest.mark.parametrize("name", ["user", "", "two words", "a/b"])
def test_scope_name_must_be_one_word_other_than_user(tmp_path, name: str) -> None:
    with pytest.raises(UserError, match="scope_name"):
        Memory(tmp_path, scope_name=name)


async def test_naming_a_scope_needs_scope_configured(tmp_path) -> None:
    mem = Memory(tmp_path)
    with pytest.raises(UserError, match="has none"):
        await mem.remember("x", scope="/srv/app")


@pytest.mark.parametrize(
    ("key", "name"),
    [
        ("/Users/me/repo/lovia", "lovia"),
        ("ssh://box/srv/app/", "app"),
        ("docker://c1:/work", "work"),
        ("C:\\work\\api", "api"),
        ("/项目/我的 应用", "我的-应用"),
        ("/", "scope"),
        ("..", "scope"),
    ],
)
def test_scope_dirname_is_slug_plus_hash(key: str, name: str) -> None:
    dirname = _scope_dirname(key)
    slug, digest = dirname.rsplit("-", 1)
    assert slug == name
    assert len(digest) == 8
    assert _scope_dirname(key) == dirname  # stable across processes
    assert _scope_dirname(key + "x") != dirname


# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------


async def test_layers_live_in_their_own_directories(tmp_path) -> None:
    mem = _scoped(tmp_path)
    await mem.remember("prefers tabs")
    await mem.remember("uses uv", scope="/srv/app")
    scope_dir = _scope_dir(tmp_path, "/srv/app")
    assert "prefers tabs" in (tmp_path / "MEMORY.md").read_text()
    assert "uses uv" in (scope_dir / "MEMORY.md").read_text()
    assert "uses uv" not in (tmp_path / "MEMORY.md").read_text()
    # The directory name can't be read back into the key, so it's recorded.
    meta = json.loads((scope_dir / "scope.json").read_text(encoding="utf-8"))
    assert meta == {"key": "/srv/app"}
    assert await mem.notes_body(scope="/srv/app") == (
        (scope_dir / "MEMORY.md").read_text()
    )


async def test_embedder_gives_every_layer_its_own_vectors(tmp_path) -> None:
    mem = _scoped(tmp_path, embedder=FakeEmbedder())
    await mem.setup(_ctx("/srv/app"))
    assert (tmp_path / "vectors.db").exists()
    assert (_scope_dir(tmp_path, "/srv/app") / "vectors.db").exists()


# ---------------------------------------------------------------------------
# setup(): prompt and tools
# ---------------------------------------------------------------------------


async def test_unscoped_run_is_exactly_a_plain_memory(tmp_path) -> None:
    plain = Memory(tmp_path / "plain")
    scoped = _scoped(tmp_path / "scoped")
    for mem in (plain, scoped):
        await mem.remember("prefers tabs")
    a = await plain.setup(_ctx(None))
    b = await scoped.setup(_ctx(None))
    assert a.instructions == b.instructions
    assert [t.parameters for t in a.tools] == [t.parameters for t in b.tools]
    assert [t.description for t in a.tools] == [t.description for t in b.tools]
    assert not (tmp_path / "scoped" / "scopes").exists()


async def test_scoped_prompt_has_two_sections_each_with_its_budget(tmp_path) -> None:
    mem = _scoped(tmp_path, notes_budget=1000)
    await mem.remember("prefers tabs")
    await mem.remember("the API lives in api/", scope="/srv/app")
    inst = await mem.setup(_ctx("/srv/app"))
    notes = (inst.instructions or "").split("\n\n", 1)[1]
    user, project = notes.split("\n\n")
    assert user.startswith("USER NOTES [")
    assert "prefers tabs" in user and "api/" not in user
    assert project.startswith("PROJECT NOTES [")
    assert "the API lives in api/" in project
    # One meter per section, each against the full budget.
    assert "/1,000 chars]" in user and "/1,000 chars]" in project


async def test_scope_name_names_the_section_and_the_about_value(tmp_path) -> None:
    mem = _scoped(tmp_path, scope_name="team")
    inst = await mem.setup(_ctx("t1"))
    assert "TEAM NOTES" in (inst.instructions or "")
    remember = next(t for t in inst.tools if t.name == "remember")
    assert remember.parameters["properties"]["about"]["enum"] == ["user", "team"]


async def test_about_parameter_only_in_a_scoped_run(tmp_path) -> None:
    mem = _scoped(tmp_path)
    plain = next(t for t in (await mem.setup(_ctx(None))).tools if t.name == "remember")
    scoped = next(
        t for t in (await mem.setup(_ctx("/srv/app"))).tools if t.name == "remember"
    )
    assert set(plain.parameters["properties"]) == {"fact", "replaces"}
    assert set(scoped.parameters["properties"]) == {"fact", "about", "replaces"}
    assert scoped.parameters["properties"]["about"]["enum"] == ["user", "project"]
    assert set(scoped.parameters["required"]) == {"fact", "about"}


async def test_scope_is_resolved_at_setup_with_the_workspace_open(tmp_path) -> None:
    # The web wiring keys scopes on the chat's workspace; it must already be
    # open when Memory.setup asks for the key.
    seen: list[Any] = []

    def scope(ctx: Any) -> str:
        seen.append(ctx.workspace)
        return "/srv/app"

    mem = Memory(tmp_path / "mem", scope=scope)
    agent = Agent(
        name="a",
        model=ScriptedProvider([text("hi")]),
        plugins=[mem],
        workspace=Workspace.local(str(tmp_path)),
    )
    await Runner.run(agent, "hello")
    assert len(seen) == 1 and seen[0] is not None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


async def test_remember_tool_writes_the_layer_about_names(tmp_path) -> None:
    mem = _scoped(tmp_path)
    provider = ScriptedProvider(
        [
            call("remember", {"fact": "prefers tabs", "about": "user"}, call_id="c1"),
            call("remember", {"fact": "uses uv", "about": "project"}, call_id="c2"),
            text("ok"),
        ]
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "remember", deps="/srv/app")
    assert await _notes(mem) == ["prefers tabs"]
    assert await _notes(mem, "/srv/app") == ["uses uv"]


async def test_forget_tool_looks_in_both_layers(tmp_path) -> None:
    mem = _scoped(tmp_path)
    await mem.remember("prefers tabs")
    await mem.remember("uses uv", scope="/srv/app")
    provider = ScriptedProvider(
        [
            call("forget", {"fact": "prefers tabs"}, call_id="c1"),
            call("forget", {"fact": "uses uv"}, call_id="c2"),
            text("ok"),
        ]
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "forget", deps="/srv/app")
    assert await _notes(mem) == []
    assert await _notes(mem, "/srv/app") == []

    # A partial text can match the wrong layer, so the scoped tool says it
    # searches both and asks for the note's full text.
    (forget,) = [
        t for t in (await mem.setup(_ctx("/srv/app"))).tools if t.name == "forget"
    ]
    assert "full text" in forget.description
    assert "the user's notes and this project's alike" in forget.description


def test_best_match_prefers_strength_then_the_later_layer() -> None:
    user = ["[2026-01] uses uv for everything"]
    scope = ["[2026-01] uses uv"]
    # Keyed match in the scope layer beats a substring hit in the user's.
    assert _best_match([user, scope], "uses uv") == (1, 0)
    # An exact user-layer fact beats a substring hit in the scope's.
    assert _best_match([user, ["uv"]], "[2026-01] uses uv for everything") == (0, 0)
    # A tie goes to the later (narrower) layer.
    assert _best_match([["a b"], ["a b"]], "a b") == (1, 0)
    assert _best_match([["x"], ["y"]], "z") is None


async def test_public_forget_stays_in_the_named_layer(tmp_path) -> None:
    mem = _scoped(tmp_path)
    await mem.remember("uses uv")
    await mem.remember("uses uv", scope="/srv/app")
    assert await mem.forget("uses uv", scope="/srv/app")
    assert await _notes(mem) == ["uses uv"]
    assert not await mem.forget("uses uv", scope="/srv/app")


async def test_recall_searches_only_its_own_scope(tmp_path) -> None:
    mem = _scoped(tmp_path, expand_query=False, summarize_recall=False)
    for key, words in (("/a", "zebra migration"), ("/b", "zebra outage")):
        await mem._archive(
            mem._tier(key),
            _msgs(("user", f"let's talk about the {words}"), ("assistant", "sure")),
            _ctx(key, run_id=key),
        )

    async def recall(key: str | None) -> str:
        inst = await mem.setup(_ctx(key))
        tool = next(t for t in inst.tools if t.name == "recall")
        return str(await tool.invoke({"query": "zebra"}, _ctx(key)))

    assert "migration" in (a := await recall("/a")) and "outage" not in a
    assert "outage" in (b := await recall("/b")) and "migration" not in b
    assert "zebra" not in await recall(None)  # the user layer's archive is apart


# ---------------------------------------------------------------------------
# Writes across the two layers, and the end of a run
# ---------------------------------------------------------------------------


async def _remember(mem: Memory, key: str, **args: str) -> str:
    """Call the scoped `remember` tool once; return what it answered."""
    inst = await mem.setup(_ctx(key))
    tool = next(t for t in inst.tools if t.name == "remember")
    return str(await tool.invoke(args, _ctx(key)))


async def test_remember_tool_never_duplicates_a_fact_across_layers(tmp_path) -> None:
    mem = _scoped(tmp_path)
    await mem.remember("prefers tabs")
    out = await _remember(mem, "/p", fact="Prefers tabs", about="project")
    assert "Already in your notes" in out
    assert await _notes(mem, "/p") == []


async def test_replaces_moves_a_fact_between_layers(tmp_path) -> None:
    # A note filed under the project turns out to be about the person: the
    # old one leaves its layer as the new one lands in the other.
    mem = _scoped(tmp_path)
    await mem.remember("uses uv", scope="/p")
    out = await _remember(
        mem, "/p", fact="uses uv everywhere", about="user", replaces="uses uv"
    )
    assert "Replaced" in out
    assert await _notes(mem) == ["uses uv everywhere"]
    assert await _notes(mem, "/p") == []


async def test_a_failed_move_leaves_the_old_note_in_place(
    tmp_path, monkeypatch
) -> None:
    # The destination is saved first: if that fails, nothing was removed.
    mem = _scoped(tmp_path)
    await mem.remember("uses uv")

    async def boom(facts: list[str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(mem._tier("/p").notes, "save", boom)
    with pytest.raises(Exception, match="disk full"):
        await _remember(
            mem, "/p", fact="uses uv here", about="project", replaces="uses uv"
        )
    assert await _notes(mem) == ["uses uv"]


async def test_each_layer_is_held_to_its_own_budget(tmp_path) -> None:
    mem = _scoped(tmp_path, notes_budget=40)
    await mem.remember("the user lives in Lisbon")
    out = await _remember(mem, "/p", fact="the user keeps two cats", about="user")
    # Refused, showing the layer that is full — the user's, not the project's.
    assert "full" in out and "USER NOTES [" in out and "Lisbon" in out
    out = await _remember(mem, "/p", fact="deploys run on Fridays", about="project")
    assert "Remembered" in out
    assert await _notes(mem, "/p") == ["deploys run on Fridays"]


async def test_a_scoped_run_is_indexed_in_its_scope(tmp_path) -> None:
    mem = _scoped(tmp_path)
    agent = Agent(name="a", model=ScriptedProvider([text("ok")]), plugins=[mem])
    await Runner.run(agent, "deploy from release branches", deps="/srv/app")
    (hit,) = await mem._tier("/srv/app").index.search("release", 5)  # type: ignore[union-attr]
    assert hit.doc.text == "USER: deploy from release branches\nASSISTANT: ok"
    assert await mem._user.index.search("release", 5) == []  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Snapshots and deliberate edits
# ---------------------------------------------------------------------------


async def test_a_session_pins_one_snapshot_per_scope(tmp_path) -> None:
    mem = _scoped(tmp_path)
    await mem.remember("alpha fact", scope="/a")
    await mem.remember("beta fact", scope="/b")
    a = await mem.setup(_ctx("/a"))
    b = await mem.setup(_ctx("/b"))  # same session, moved to another scope
    assert "alpha fact" in (a.instructions or "") and "beta" not in (
        a.instructions or ""
    )
    assert "beta fact" in (b.instructions or "") and "alpha" not in (
        b.instructions or ""
    )


async def test_scoped_snapshot_survives_writes_within_the_session(tmp_path) -> None:
    mem = _scoped(tmp_path)
    provider = ScriptedProvider(
        [
            call("remember", {"fact": "uses uv", "about": "project"}, call_id="c1"),
            text("ok"),
            text("hi"),
        ]
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    session = InMemorySession()
    for prompt in ("remember uv", "again"):
        await Runner.run(agent, prompt, session=session, session_id="s1", deps="/p")
    assert provider.calls[2][0].content == provider.calls[0][0].content


async def test_replace_notes_in_a_scope_unpins_sessions(tmp_path) -> None:
    mem = _scoped(tmp_path)
    await mem.setup(_ctx("/p"))
    assert await mem.replace_notes("- edited by hand", scope="/p") == (
        await mem.notes_body(scope="/p")
    )
    assert mem._snapshots == {}
    inst = await mem.setup(_ctx("/p"))
    assert "edited by hand" in (inst.instructions or "")
    assert await mem.notes_body() == ""


async def test_clear_scope_removes_notes_and_archive(tmp_path) -> None:
    mem = _scoped(tmp_path, expand_query=False, summarize_recall=False)
    await mem.remember("prefers tabs")
    await mem.remember("uses uv", scope="/p")
    await mem._archive(
        mem._tier("/p"),
        _msgs(("user", "the zebra plan"), ("assistant", "ok")),
        _ctx("/p"),
    )
    inst = await mem.setup(_ctx("/p"))  # a run that started before the clear
    recall = next(t for t in inst.tools if t.name == "recall")

    await mem.clear_scope("/p")

    assert await mem.notes_body(scope="/p") == ""
    assert await _notes(mem) == ["prefers tabs"]  # the user layer is untouched
    assert mem._snapshots == {}
    assert "nothing relevant" in str(
        await recall.invoke({"query": "zebra"}, _ctx("/p"))
    )
    # The scope works afresh, the in-flight run's tools included.
    await mem.remember("uses pdm", scope="/p")
    assert await _notes(mem, "/p") == ["uses pdm"]
    meta = json.loads((_scope_dir(tmp_path, "/p") / "scope.json").read_text())
    assert meta == {"key": "/p"}


async def test_a_run_ending_after_a_clear_lands_in_the_fresh_stores(tmp_path) -> None:
    # The run set up before the clear; its archive write must not go to the
    # index object whose file the clear deleted.
    mem = _scoped(tmp_path)
    await mem._archive(
        mem._tier("/p"),
        _msgs(("user", "the zebra plan"), ("assistant", "ok")),
        _ctx("/p"),
    )

    @tool
    async def clear_now() -> str:
        """Clear the project mid-run."""
        await mem.clear_scope("/p")
        return "cleared"

    provider = ScriptedProvider([call("clear_now", {}, call_id="c1"), text("ok")])
    agent = Agent(name="a", model=provider, tools=[clear_now], plugins=[mem])
    await Runner.run(agent, "the quokka plan", deps="/p")

    assert len(await mem._tier("/p").index.search("quokka", 5)) == 1  # type: ignore[union-attr]
    assert await mem._tier("/p").index.search("zebra", 5) == []  # type: ignore[union-attr]
