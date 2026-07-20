"""Tests for the Memory plugin: Notes (hot) + Archive (cold), tools, curation.

The unit tests are network-free: they drive the runner with the scripted
provider and monkeypatch the LLM curation side-queries. The opt-in live e2e
tests exercise the real provider configured in ``.env``::

    LOVIA_LIVE_TESTS=1 uv run pytest tests/plugins/test_memory.py -k live
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lovia import Agent, ModelSettings, Runner
from lovia.exceptions import UserError
from lovia.plugins.memory import plugin as plugin_mod
from lovia.plugins.memory.index import Doc, Hit, HybridIndex, KeywordIndex
from lovia.plugins.memory.plugin import (
    FileNotesStore,
    Memory,
    _drop_fact,
    _format_facts,
    _hit_line,
    _meter,
    _normalize_fact,
    _parse_facts,
    _RunDigest,
)
from lovia.plugins.memory.vector import VectorIndex
from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
)

from ..scripted_provider import ScriptedProvider, call, text


def _msgs(*pairs: tuple[str, str]) -> list:
    """Build a simple user/assistant transcript from (role, content) pairs."""
    entries: list = []
    for role, content in pairs:
        if role == "user":
            entries.append(InputEntry(role="user", content=content))
        else:
            entries.append(AssistantTextEntry(content=content))
    return entries


def _ctx(run_id: str = "r1", session_id: str | None = "s1") -> SimpleNamespace:
    """The slice of RunContext the curation path reads."""
    return SimpleNamespace(
        run_id=run_id, session_id=session_id, agent=SimpleNamespace(model="test-model")
    )


class SpyIndex:
    """Index that records calls and replays canned hits."""

    def __init__(self, hits: list[Hit] | None = None) -> None:
        self.hits = hits or []
        self.added: list[Doc] = []
        self.removed: list[str] = []
        self.queries: list[str] = []

    async def add(self, docs: list[Doc]) -> None:
        self.added.extend(docs)

    async def remove(self, ids: list[str]) -> None:
        self.removed.extend(ids)

    async def search(self, query: str, k: int = 5) -> list[Hit]:
        self.queries.append(query)
        return self.hits[:k]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_meter_format() -> None:
    assert _meter(1474, 2000) == "[74% — 1,474/2,000 chars]"
    assert _meter(0, 0) == "[0% — 0/0 chars]"
    assert _meter(50, 100) == "[50% — 50/100 chars]"


def test_parse_format_roundtrip() -> None:
    body = "- one\n-  two \n  - three\nnot a bullet"
    assert _parse_facts(body) == ["one", "two", "three"]
    assert _format_facts(["a", "b"]) == "- a\n- b"
    assert _format_facts([]) == ""


def test_normalize_fact() -> None:
    assert _normalize_fact("  hello   world \n again ") == "hello world again"
    assert _normalize_fact("   ") == ""


def test_drop_fact_strategies() -> None:
    assert _drop_fact(["a", "b"], "a") == ["b"]
    assert _drop_fact(["Apple"], "apple") == []  # case-insensitive
    assert _drop_fact(["I like apples a lot"], "apples") == []  # substring
    assert _drop_fact(["x"], "nope") == ["x"]  # no match → unchanged


def test_hit_line_renders_date() -> None:
    hit = Hit(doc=Doc(id="a", text="saw Rex", when=86400.0 * 365 * 30), score=1.0)
    line = _hit_line(hit)
    assert line.startswith("[") and line.endswith("] saw Rex")
    # No timestamp → bare text.
    assert _hit_line(Hit(doc=Doc(id="a", text="bare"), score=0.0)) == "bare"


# ---------------------------------------------------------------------------
# FileNotesStore (hot tier persistence)
# ---------------------------------------------------------------------------


async def test_notes_store_roundtrip(tmp_path) -> None:
    store = FileNotesStore(tmp_path / "nested" / "MEMORY.md")
    assert await store.load() == []  # missing file → empty
    await store.save(["one", "two"])
    assert await store.load() == ["one", "two"]
    assert (tmp_path / "nested" / "MEMORY.md").read_text() == "- one\n- two"
    await store.save([])
    assert await store.load() == []


async def test_notes_store_tolerates_hand_edits(tmp_path) -> None:
    path = tmp_path / "MEMORY.md"
    path.write_text("# My notes\n\n- keep me\nprose line\n  - indented too\n")
    store = FileNotesStore(path)
    assert await store.load() == ["keep me", "indented too"]


# ---------------------------------------------------------------------------
# Notes policy (plugin-side): dedup, normalization, locking
# ---------------------------------------------------------------------------


async def test_add_facts_normalizes_and_dedups(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    assert await mem._add_facts(["I prefer tabs.  "]) == 1
    assert await mem._add_facts(["  I prefer   tabs. "]) == 0  # whitespace dup
    assert await mem._add_facts(["I PREFER TABS."]) == 0  # case-insensitive dup
    assert await mem._add_facts(["My name is\nAlice"]) == 1  # multi-line → one line
    assert await mem._add_facts(["   "]) == 0  # blank → ignored
    assert await mem._notes_store().load() == ["I prefer tabs.", "My name is Alice"]


async def test_concurrent_adds_are_serialized(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await asyncio.gather(*(mem._add_facts([f"fact number {i}"]) for i in range(50)))
    facts = await mem._notes_store().load()
    assert len(facts) == 50
    assert set(facts) == {f"fact number {i}" for i in range(50)}


async def test_public_remember_and_forget(tmp_path) -> None:
    # remember/forget are public: code can seed and clean Notes without a
    # model in the loop, with the same semantics as the tools.
    mem = Memory(tmp_path / "mem", index=None)
    assert await mem.remember("user speaks French") is True
    assert await mem.remember("USER SPEAKS FRENCH") is False  # dup
    assert await mem.forget("speaks french") is True  # substring match
    assert await mem.forget("speaks french") is False  # already gone
    assert await mem.forget("   ") is False  # blank → no-op
    assert await mem._notes_store().load() == []


async def test_notes_body_and_replace_notes(tmp_path) -> None:
    # The editor seam: read the canonical body, replace it wholesale with the
    # same normalization/dedup policy every other Notes write applies.
    mem = Memory(tmp_path / "mem", index=None)
    assert await mem.notes_body() == ""
    await mem.remember("likes jazz")
    assert await mem.notes_body() == "- likes jazz"

    stored = await mem.replace_notes(
        "# a heading, ignored\n"
        "- uses  vim   daily\n"
        "not a bullet, ignored\n"
        "- USES VIM DAILY\n"  # case-insensitive dup of the one above
        "-not a bullet either (no space)\n"
        "- \n"  # empty fact → ignored
        "- speaks French\n"
    )
    assert stored == "- uses vim daily\n- speaks French"
    assert await mem.notes_body() == stored
    assert await mem._notes_store().load() == ["uses vim daily", "speaks French"]

    # Replacing with an empty body clears the notes.
    assert await mem.replace_notes("") == ""
    assert await mem.notes_body() == ""


# ---------------------------------------------------------------------------
# Construction: the three-step ladder (default / embedder= / index=)
# ---------------------------------------------------------------------------


def test_default_builds_notes_and_keyword_index(tmp_path) -> None:
    mem = Memory(tmp_path / "mem")
    assert isinstance(mem.notes, FileNotesStore)
    assert isinstance(mem.index, KeywordIndex)
    assert mem._should_expand()  # lexical-only default → expansion on


async def test_embedder_upgrades_default_to_hybrid(tmp_path) -> None:
    class Emb:
        id = "fake:v1"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    mem = Memory(tmp_path / "mem", embedder=Emb())
    assert isinstance(mem.index, HybridIndex)
    kinds = {type(arm) for arm in mem.index.indexes}
    assert kinds == {KeywordIndex, VectorIndex}
    assert not mem._should_expand()  # semantic arm present → auto-expansion off
    # Both arms live under the root (db files themselves are created lazily).
    assert (tmp_path / "mem").is_dir()
    await mem.index.add([Doc(id="a", text="hello")])
    assert (tmp_path / "mem" / "archive.db").exists()
    assert (tmp_path / "mem" / "vectors.db").exists()


def test_custom_index_used_verbatim(tmp_path) -> None:
    spy = SpyIndex()
    mem = Memory(tmp_path / "mem", index=spy)
    assert mem.index is spy
    assert not mem._should_expand()  # unknown engine → no auto-expansion
    assert Memory(tmp_path / "m2", index=spy, expand_query=True)._should_expand()


def test_index_none_disables_cold_tier(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    assert mem.index is None
    assert not (tmp_path / "mem" / "archive.db").exists()


def test_embedder_with_custom_index_is_an_error(tmp_path) -> None:
    class Emb:
        id = "fake:v1"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] for _ in texts]

    with pytest.raises(UserError, match="not both"):
        Memory(tmp_path / "mem", index=SpyIndex(), embedder=Emb())


def test_custom_notes_store_used_verbatim(tmp_path) -> None:
    notes = FileNotesStore(tmp_path / "elsewhere.md")
    mem = Memory(tmp_path / "mem", notes=notes, index=None)
    assert mem.notes is notes


def test_expand_query_forced_on_and_off(tmp_path) -> None:
    assert not Memory(tmp_path / "a", expand_query=False)._should_expand()
    assert Memory(tmp_path / "b", expand_query=True)._should_expand()


# ---------------------------------------------------------------------------
# setup() contributions
# ---------------------------------------------------------------------------


async def test_setup_instructions_include_notes_and_tools(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=SpyIndex())
    await mem._add_facts(["user prefers dark mode"])
    inst = await mem.setup()
    assert {t.name for t in inst.tools} == {"remember", "forget", "recall"}
    assert "NOTES" in inst.instructions
    assert "user prefers dark mode" in inst.instructions
    assert "recall" in inst.instructions
    assert inst.hooks is not None


async def test_setup_without_index(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem._add_facts(["durable note"])
    inst = await mem.setup()
    assert {t.name for t in inst.tools} == {"remember", "forget"}  # no recall
    assert "durable note" in inst.instructions  # notes are always injected
    assert "remember" in inst.instructions  # usage guidance still present
    assert "recall" not in inst.instructions  # no cold-tier guidance


# ---------------------------------------------------------------------------
# Tools through a scripted run
# ---------------------------------------------------------------------------


async def test_system_prompt_carries_notes(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None, auto_curate=False)
    await mem._add_facts(["the sky is blue"])
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "go")
    system = provider.calls[0][0]
    assert system.role == "system"
    assert "the sky is blue" in (system.content or "")
    assert "remember" in (system.content or "")


async def test_remember_tool_persists_via_run(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("remember", {"fact": "user likes vim"}, call_id="c1"), text("Saved!")]
    )
    mem = Memory(tmp_path / "mem", index=None, auto_curate=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "remember that I like vim")
    assert "user likes vim" in await mem._notes_store().load()
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("Remembered" in r.output for r in tool_results)


async def test_remember_tool_reports_duplicates(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("remember", {"fact": "user likes vim"}, call_id="c1"), text("ok")]
    )
    mem = Memory(tmp_path / "mem", index=None, auto_curate=False)
    await mem._add_facts(["user likes vim"])
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "again")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("Already in your notes" in r.output for r in tool_results)


async def test_forget_tool_via_run(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("forget", {"fact": "user likes vim"}, call_id="c1"), text("Done")]
    )
    mem = Memory(tmp_path / "mem", index=None, auto_curate=False)
    await mem._add_facts(["user likes vim"])
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "forget that I like vim")
    assert await mem._notes_store().load() == []


async def test_forget_tool_reports_no_match(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("forget", {"fact": "nothing like this"}, call_id="c1"), text("ok")]
    )
    mem = Memory(tmp_path / "mem", index=None, auto_curate=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "forget")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("No matching note" in r.output for r in tool_results)


async def test_recall_tool_returns_raw_hits(tmp_path) -> None:
    index = KeywordIndex(":memory:")
    await index.add([Doc(id="d1", text="my dog's name is Rex")])
    provider = ScriptedProvider(
        [call("recall", {"query": "dog name"}, call_id="c1"), text("Rex.")]
    )
    mem = Memory(
        tmp_path / "mem",
        index=index,
        auto_curate=False,
        summarize_recall=False,
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "what's my dog's name?")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("Rex" in r.output for r in tool_results)


async def test_recall_tool_summarizes_when_enabled(tmp_path, monkeypatch) -> None:
    seen = {}

    async def fake_summarize(hits, query, model):
        seen["query"] = query
        seen["n_hits"] = len(hits)
        return "SUMMARY: your dog is Rex"

    monkeypatch.setattr(plugin_mod, "_summarize", fake_summarize)
    index = SpyIndex([Hit(doc=Doc(id="d1", text="my dog Rex"), score=1.0)])
    provider = ScriptedProvider(
        [call("recall", {"query": "dog"}, call_id="c1"), text("done")]
    )
    mem = Memory(tmp_path / "mem", index=index, auto_curate=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "dog?")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("SUMMARY: your dog is Rex" in r.output for r in tool_results)
    assert seen == {"query": "dog", "n_hits": 1}


async def test_recall_summary_failure_falls_back_to_raw_hits(
    tmp_path, monkeypatch
) -> None:
    async def boom(*a, **k):
        raise RuntimeError("summarizer exploded")

    monkeypatch.setattr(plugin_mod, "_summarize", boom)
    index = SpyIndex([Hit(doc=Doc(id="d1", text="my dog Rex"), score=1.0)])
    provider = ScriptedProvider(
        [call("recall", {"query": "dog"}, call_id="c1"), text("done")]
    )
    mem = Memory(tmp_path / "mem", index=index, auto_curate=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "dog?")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("my dog Rex" in r.output for r in tool_results)


async def test_recall_tool_handles_no_hits(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("recall", {"query": "nonexistent"}, call_id="c1"), text("nothing")]
    )
    mem = Memory(
        tmp_path / "mem",
        index=SpyIndex(),
        auto_curate=False,
        summarize_recall=False,
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "?")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("nothing relevant" in r.output for r in tool_results)


async def test_recall_expands_query_when_enabled(tmp_path, monkeypatch) -> None:
    async def fake_expand(query, model):
        assert query == "car"
        return ["automobile", "汽车"]

    monkeypatch.setattr(plugin_mod, "_expand", fake_expand)
    index = SpyIndex([Hit(doc=Doc(id="d1", text="bought a car"), score=1.0)])
    provider = ScriptedProvider(
        [call("recall", {"query": "car"}, call_id="c1"), text("done")]
    )
    mem = Memory(
        tmp_path / "mem",
        index=index,
        expand_query=True,
        auto_curate=False,
        summarize_recall=False,
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "car?")
    # The index saw the original query plus the expansion terms.
    assert index.queries == ["car automobile 汽车"]


async def test_recall_expansion_failure_searches_raw_query(
    tmp_path, monkeypatch
) -> None:
    async def boom(query, model):
        raise RuntimeError("expander exploded")

    monkeypatch.setattr(plugin_mod, "_expand", boom)
    index = SpyIndex([Hit(doc=Doc(id="d1", text="bought a car"), score=1.0)])
    provider = ScriptedProvider(
        [call("recall", {"query": "car"}, call_id="c1"), text("done")]
    )
    mem = Memory(
        tmp_path / "mem",
        index=index,
        expand_query=True,
        auto_curate=False,
        summarize_recall=False,
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "car?")
    assert index.queries == ["car"]
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("bought a car" in r.output for r in tool_results)


async def test_recall_default_does_not_expand_on_custom_index(
    tmp_path, monkeypatch
) -> None:
    calls = {"n": 0}

    async def fake_expand(query, model):
        calls["n"] += 1
        return ["nope"]

    monkeypatch.setattr(plugin_mod, "_expand", fake_expand)
    index = SpyIndex([Hit(doc=Doc(id="d1", text="x"), score=1.0)])
    provider = ScriptedProvider(
        [call("recall", {"query": "q"}, call_id="c1"), text("done")]
    )
    mem = Memory(
        tmp_path / "mem", index=index, auto_curate=False, summarize_recall=False
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "?")
    assert calls["n"] == 0
    assert index.queries == ["q"]


# ---------------------------------------------------------------------------
# End-of-run curation: digest → notes + archive
# ---------------------------------------------------------------------------


async def test_run_completed_digests_and_ingests(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_digest(entries, current, model):
        captured["current"] = current
        return _RunDigest(
            facts=["the user is a pirate"],
            summary="Talked like pirates about sailing.",
        )

    monkeypatch.setattr(plugin_mod, "_digest", fake_digest)
    index = SpyIndex()
    mem = Memory(tmp_path / "mem", index=index)
    provider = ScriptedProvider([text("Arr, hello matey!")])
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "ahoy there sailor", session_id="s1")

    # The digest fact was promoted into Notes (with the empty current passed).
    assert "the user is a pirate" in await mem._notes_store().load()
    assert captured["current"] == ""
    # Both messages and the episode summary landed in the index.
    kinds = [d.meta["kind"] for d in index.added]
    assert kinds.count("message") == 2  # user + assistant
    assert kinds.count("summary") == 1
    summary_doc = next(d for d in index.added if d.meta["kind"] == "summary")
    assert summary_doc.text == "Talked like pirates about sailing."
    assert summary_doc.meta["session_id"] == "s1"
    assert summary_doc.id.endswith(":summary")
    assert all(d.when > 0 for d in index.added)


async def test_curate_in_background_defers_and_drains(tmp_path, monkeypatch) -> None:
    # With curate_in_background the run returns while the digest is still
    # parked on the gate (inline mode would deadlock here); drain() settles it.
    gate = asyncio.Event()

    async def fake_digest(entries, current, model):
        await gate.wait()
        return _RunDigest(facts=["works at Dawn Café"], summary="")

    monkeypatch.setattr(plugin_mod, "_digest", fake_digest)
    mem = Memory(tmp_path / "mem", index=None, curate_in_background=True)
    agent = Agent(name="a", model=ScriptedProvider([text("hi")]), plugins=[mem])
    await Runner.run(agent, "hello")

    assert mem._curation_tasks  # curation is in flight, not done inline
    assert await mem._notes_store().load() == []
    gate.set()
    await mem.drain()
    assert not mem._curation_tasks
    assert "works at Dawn Café" in await mem._notes_store().load()


async def test_auto_curate_false_ingests_messages_only(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_digest(*a, **k):
        calls["n"] += 1
        return _RunDigest()

    monkeypatch.setattr(plugin_mod, "_digest", fake_digest)
    index = SpyIndex()
    mem = Memory(tmp_path / "mem", index=index, auto_curate=False)
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "hello world uniquexyz", session_id="s1")

    assert calls["n"] == 0
    assert await mem._notes_store().load() == []
    # Archive ingest happens regardless of auto_curate — raw messages only.
    assert [d.meta["kind"] for d in index.added] == ["message", "message"]


async def test_digest_failure_still_ingests_messages(tmp_path, monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("digest exploded")

    monkeypatch.setattr(plugin_mod, "_digest", boom)
    index = SpyIndex()
    mem = Memory(tmp_path / "mem", index=index)
    provider = ScriptedProvider([text("still fine")])
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "hello", session_id="s1")
    assert result.output == "still fine"
    assert await mem._notes_store().load() == []
    assert [d.meta["kind"] for d in index.added] == ["message", "message"]


async def test_ingest_failure_still_curates_notes(tmp_path, monkeypatch) -> None:
    async def fake_digest(entries, current, model):
        return _RunDigest(facts=["a durable fact"], summary="s")

    monkeypatch.setattr(plugin_mod, "_digest", fake_digest)

    class BrokenIndex(SpyIndex):
        async def add(self, docs: list[Doc]) -> None:
            raise RuntimeError("index exploded")

    mem = Memory(tmp_path / "mem", index=BrokenIndex())
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "hello")
    assert result.output == "ok"
    assert "a durable fact" in await mem._notes_store().load()


async def test_consolidation_triggers_over_budget(tmp_path, monkeypatch) -> None:
    async def fake_digest(entries, current, model):
        return _RunDigest(facts=["a very long fact that blows the tiny budget open"])

    async def fake_consolidate(body, max_chars, model):
        assert len(body) > max_chars
        return ["compact fact"]

    monkeypatch.setattr(plugin_mod, "_digest", fake_digest)
    monkeypatch.setattr(plugin_mod, "_consolidate", fake_consolidate)

    mem = Memory(tmp_path / "mem", index=None, notes_budget=20)
    provider = ScriptedProvider([text("ok")])
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "go")
    assert await mem._notes_store().load() == ["compact fact"]


async def test_remember_during_consolidation_is_not_lost(tmp_path, monkeypatch) -> None:
    # Consolidation rewrites the whole fact list around a slow model call; a
    # remember() landing mid-flight must block on the lock and survive, not be
    # overwritten by the consolidated save.
    started = asyncio.Event()

    async def slow_consolidate(body, max_chars, model):
        started.set()
        await asyncio.sleep(0.05)
        return ["compact fact"]

    monkeypatch.setattr(plugin_mod, "_consolidate", slow_consolidate)
    mem = Memory(tmp_path / "mem", index=None, notes_budget=10)
    await mem._add_facts(["a very long fact exceeding the tiny budget"])

    task = asyncio.create_task(mem._consolidate_if_over_budget(_ctx()))
    await started.wait()
    await mem.remember("landed mid-flight")
    await task
    facts = await mem._notes_store().load()
    assert "compact fact" in facts
    assert "landed mid-flight" in facts


async def test_model_override_used_for_curation(tmp_path, monkeypatch) -> None:
    seen = {}

    async def fake_digest(entries, current, model):
        seen["model"] = model
        return _RunDigest()

    monkeypatch.setattr(plugin_mod, "_digest", fake_digest)
    mem = Memory(tmp_path / "mem", index=None, model="openai:some-cheap-model")
    provider = ScriptedProvider([text("hi")])
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "go")
    assert seen["model"] == "openai:some-cheap-model"


# ---------------------------------------------------------------------------
# Deterministic ids → idempotent re-ingest
# ---------------------------------------------------------------------------


def test_run_docs_ids_are_deterministic(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    entries = _msgs(("user", "hello"), ("assistant", "hi there"))
    digest = _RunDigest(summary="a summary")
    docs1 = mem._run_docs(entries, _ctx(run_id="r1"), digest)
    docs2 = mem._run_docs(entries, _ctx(run_id="r1"), digest)
    assert [d.id for d in docs1] == ["r1:0", "r1:1", "r1:summary"]
    assert [d.id for d in docs1] == [d.id for d in docs2]
    # A different run gets different ids; no session_id → no meta key.
    docs3 = mem._run_docs(entries, _ctx(run_id="r2", session_id=None), None)
    assert [d.id for d in docs3] == ["r2:0", "r2:1"]
    assert all("session_id" not in d.meta for d in docs3)


async def test_reingest_upserts_instead_of_duplicating(tmp_path) -> None:
    # A resumed run re-runs _curate with the same run_id: deterministic ids
    # make the second ingest an upsert, not a duplicate.
    index = KeywordIndex(":memory:")
    mem = Memory(tmp_path / "mem", index=index, auto_curate=False)
    entries = _msgs(("user", "zebra fact one"), ("assistant", "noted zebra"))
    await mem._curate(index, entries, _ctx(run_id="r1"))
    await mem._curate(index, entries, _ctx(run_id="r1"))
    hits = await index.search("zebra", k=10)
    assert len(hits) == 2  # one per message, not four


def test_run_docs_skips_tool_and_system_entries(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    entries = [
        InputEntry(role="system", content="system prompt should not be archived"),
        InputEntry(role="user", content="user question echotoken"),
        ToolCallEntry(call_id="c1", name="foo", arguments="{}"),
        ToolResultEntry(call_id="c1", output="tool output should not be archived"),
        AssistantTextEntry(content="assistant answer echotoken"),
    ]
    docs = mem._run_docs(entries, _ctx(), None)
    assert [d.text for d in docs] == [
        "user question echotoken",
        "assistant answer echotoken",
    ]


# ---------------------------------------------------------------------------
# Live e2e (opt-in; uses the provider configured in .env)
# ---------------------------------------------------------------------------


def _load_env_file() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _live_model() -> str:
    # Gate before loading: a normal run must not pull real .env keys into
    # os.environ (and the opt-in itself must come from the shell, not .env).
    if os.getenv("LOVIA_LIVE_TESTS") != "1":
        pytest.skip("opt-in: set LOVIA_LIVE_TESTS=1 to run live provider tests")
    _load_env_file()
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not configured")
    return os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5.5")


@pytest.mark.live_provider
async def test_live_notes_persist_across_sessions(tmp_path) -> None:
    model = _live_model()

    def make_agent() -> Agent:
        return Agent(
            name="assistant",
            model=f"openai:{model}",
            instructions="You are concise and helpful.",
            settings=ModelSettings(temperature=0),
            plugins=[Memory(tmp_path / "mem")],
        )

    # Run 1: state a durable preference (the digest should promote it).
    await Runner.run(
        make_agent(),
        "Please remember that I strongly prefer Python over JavaScript for all "
        "code examples you give me.",
        session_id="live-pref-1",
    )
    body = (tmp_path / "mem" / "MEMORY.md").read_text().lower()
    assert "python" in body, f"preference not promoted into Notes; got: {body!r}"

    # Run 2: a fresh agent over the same Notes file should already know it.
    res2 = await Runner.run(
        make_agent(),
        "What programming language will you use for code examples for me, and why?",
        session_id="live-pref-2",
    )
    assert "python" in str(res2.output).lower()


@pytest.mark.live_provider
async def test_live_archive_recall_with_expansion(tmp_path) -> None:
    model = _live_model()
    index = KeywordIndex(tmp_path / "archive.db")
    mem = Memory(
        tmp_path / "mem",
        index=index,
        auto_curate=False,
        expand_query=True,
    )
    await mem._curate(
        index,
        _msgs(
            (
                "user",
                "I'm planning a trip to Kyoto in November to see the autumn "
                "maple leaves.",
            ),
            (
                "assistant",
                "Kyoto in November is beautiful for koyo (autumn foliage). "
                "Tofuku-ji and Arashiyama are great spots.",
            ),
        ),
        _ctx(run_id="old-trip", session_id="old-trip"),
    )
    agent = Agent(
        name="assistant",
        model=f"openai:{model}",
        instructions=(
            "Answer questions about the user's past conversations. Use the "
            "`recall` tool to look things up before answering."
        ),
        settings=ModelSettings(temperature=0),
        plugins=[mem],
    )
    res = await Runner.run(
        agent,
        "Where was I planning to travel, and what did I want to see there?",
        session_id="live-recall",
    )
    assert "kyoto" in str(res.output).lower()
