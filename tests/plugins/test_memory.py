"""Tests for the Memory plugin: Notes (hot) + Archive (cold) and their tools.

The unit tests are network-free: they drive the runner with the scripted
provider and monkeypatch recall's LLM side-queries. The opt-in live e2e
tests exercise the real provider configured in ``.env``::

    LOVIA_LIVE_TESTS=1 uv run pytest tests/plugins/test_memory.py -k live
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lovia import Agent, InMemorySession, ModelSettings, Runner, tool
from lovia.exceptions import UserError
from lovia.tools import Tool
from lovia.plugins.memory import plugin as plugin_mod
from lovia.plugins.memory.index import Doc, Hit, HybridIndex, KeywordIndex
from lovia.plugins.memory.plugin import (
    FileNotesStore,
    Memory,
    _fact_key,
    _format_facts,
    _hit_line,
    _match,
    _meter,
    _normalize_fact,
    _parse_facts,
    _run_doc,
    _stamp,
    _strip_date,
)
from lovia.plugins.memory.vector import VectorIndex
from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
)

from ..scripted_provider import ScriptedProvider, call, text


def _bare(facts: list[str]) -> list[str]:
    """The facts without their ``[YYYY-MM]`` stamps, for content assertions."""
    return [_strip_date(f) for f in facts]


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
    """The slice of RunContext the archive path reads."""
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


def test_match_strategies() -> None:
    assert _match(["a", "b"], "b") == (0, 1)  # exact
    assert _match(["Apple"], "apple") == (1, 0)  # case-insensitive
    assert _match(["I like apples a lot"], "apples") == (2, 0)  # substring
    assert _match(["x"], "nope") is None


def test_match_ignores_date_prefix() -> None:
    # Dates never block a match: stamped note, bare target and vice versa.
    assert _match(["[2026-01] user likes vim"], "user likes vim") == (1, 0)
    assert _match(["[2026-01] user likes vim"], "[2026-06] USER LIKES VIM") == (1, 0)
    assert _match(["user likes vim"], "[2026-06] likes vim") == (2, 0)  # substring
    # A date-only target matches nothing (its key is empty).
    assert _match(["[2026-01] x"], "[2026-02]") is None


def test_strip_date_fact_key_and_stamp(monkeypatch) -> None:
    assert _strip_date("[2026-07] likes jazz") == "likes jazz"
    assert _strip_date("no date here") == "no date here"
    assert _strip_date("[199-07] malformed stays") == "[199-07] malformed stays"
    assert _fact_key("[2026-07]  Likes   Jazz ") == "likes jazz"
    assert _fact_key("[2026-07]") == ""
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    assert _stamp("likes jazz") == "[2026-01] likes jazz"
    assert _stamp("[2025-01] likes jazz") == "[2025-01] likes jazz"  # kept


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


async def test_remember_normalizes_and_dedups(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    mem = Memory(tmp_path / "mem", index=None)
    assert await mem.remember("I prefer tabs.  ") is True
    assert await mem.remember("  I prefer   tabs. ") is False  # whitespace dup
    assert await mem.remember("I PREFER TABS.") is False  # case-insensitive dup
    assert await mem.remember("My name is\nAlice") is True  # multi-line → one line
    assert await mem.remember("   ") is False  # blank → ignored
    facts = await mem._user.notes.load()
    assert _bare(facts) == ["I prefer tabs.", "My name is Alice"]
    # Everything written got stamped with the (frozen) current month.
    assert all(f.startswith("[2026-01] ") for f in facts)


async def test_remember_stamps_and_dedups_across_months(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("likes jazz")
    # The same fact arriving under an older stamp (a hand-pasted note) is a
    # duplicate, not a new fact.
    assert await mem.remember("[2025-12] likes jazz") is False
    # An incoming fact that already carries a stamp keeps it.
    assert await mem.remember("[2025-11] drinks tea") is True
    assert "[2025-11] drinks tea" in await mem._user.notes.load()
    # A date-only line is as good as blank.
    assert await mem.remember("[2025-10]") is False


async def test_remember_replaces_the_note_it_supersedes(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("uses pip")
    await mem.remember("likes jazz")
    assert await mem.remember("uses uv, not pip", replaces="uses pip") is True
    assert _bare(await mem._user.notes.load()) == ["likes jazz", "uses uv, not pip"]
    # No such note: nothing changes — the fact is not added beside the old one.
    assert await mem.remember("uses poetry", replaces="uses conda") is False
    assert _bare(await mem._user.notes.load()) == ["likes jazz", "uses uv, not pip"]
    # A blank fact never turns `replaces` into a forget.
    assert await mem.remember("  ", replaces="likes jazz") is False
    assert len(await mem._user.notes.load()) == 2


async def test_public_remember_is_not_held_to_the_budget(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None, notes_budget=10)
    assert await mem.remember("a fact far longer than ten characters") is True


async def test_concurrent_adds_are_serialized(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await asyncio.gather(*(mem.remember(f"fact number {i}") for i in range(50)))
    facts = await mem._user.notes.load()
    assert len(facts) == 50
    assert set(_bare(facts)) == {f"fact number {i}" for i in range(50)}


async def test_public_remember_and_forget(tmp_path) -> None:
    # remember/forget are public: code can seed and clean Notes without a
    # model in the loop, with the same semantics as the tools.
    mem = Memory(tmp_path / "mem", index=None)
    assert await mem.remember("user speaks French") is True
    assert await mem.remember("USER SPEAKS FRENCH") is False  # dup
    assert await mem.forget("speaks french") is True  # substring match
    assert await mem.forget("speaks french") is False  # already gone
    assert await mem.forget("   ") is False  # blank → no-op
    assert await mem._user.notes.load() == []


async def test_notes_body_and_replace_notes(tmp_path, monkeypatch) -> None:
    # The editor seam: read the canonical body, replace it wholesale with the
    # same normalization/dedup policy every other Notes write applies.
    mem = Memory(tmp_path / "mem", index=None)
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    assert await mem.notes_body() == ""
    await mem.remember("likes jazz")
    assert await mem.notes_body() == "- [2026-01] likes jazz"

    stored = await mem.replace_notes(
        "# a heading, ignored\n"
        "- uses  vim   daily\n"
        "not a bullet, ignored\n"
        "- USES VIM DAILY\n"  # case-insensitive dup of the one above
        "-not a bullet either (no space)\n"
        "- \n"  # empty fact → ignored
        "- [2025-03] speaks French\n"  # already dated → stamp kept
    )
    assert stored == "- [2026-01] uses vim daily\n- [2025-03] speaks French"
    assert await mem.notes_body() == stored
    assert _bare(await mem._user.notes.load()) == ["uses vim daily", "speaks French"]

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
    await mem.remember("user prefers dark mode")
    inst = await mem.setup(_ctx(session_id=None))
    assert {t.name for t in inst.tools} == {"remember", "forget", "recall"}
    assert "NOTES" in inst.instructions
    assert "user prefers dark mode" in inst.instructions
    # What recall does is its description's job, not the section's.
    assert "`recall(" not in inst.instructions
    assert inst.hooks is not None


async def test_setup_without_index(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("durable note")
    inst = await mem.setup(_ctx(session_id=None))
    assert {t.name for t in inst.tools} == {"remember", "forget"}  # no recall
    assert inst.hooks is None  # nothing to index, so nothing runs at run end
    assert "durable note" in inst.instructions  # notes are always injected
    assert "remember" in inst.instructions  # usage guidance still present
    assert "recall" not in inst.instructions  # no cold-tier guidance


# ---------------------------------------------------------------------------
# Session-pinned Notes snapshot (keeps the cached prompt prefix byte-stable)
# ---------------------------------------------------------------------------


async def _two_runs(mem: Memory, *, session_id: str | None = "s1") -> list[str]:
    """Run 1 remembers a fact; return both runs' system prompts."""
    provider = ScriptedProvider(
        [
            call("remember", {"fact": "user likes vim"}, call_id="c1"),
            text("ok"),
            text("hi"),
        ]
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    session = InMemorySession() if session_id is not None else None
    await Runner.run(
        agent, "remember I like vim", session=session, session_id=session_id
    )
    await Runner.run(agent, "hello again", session=session, session_id=session_id)
    return [provider.calls[i][0].content or "" for i in (0, 2)]


async def test_notes_pinned_within_a_session(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    first, second = await _two_runs(mem)
    assert second == first  # the cached prefix survives the write
    assert "user likes vim" not in second
    assert "user likes vim" in _bare(await mem._user.notes.load())


async def test_notes_refresh_once_the_session_idles(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("old fact")
    first = await mem.setup(_ctx())
    await mem.remember("new fact")
    text_, used = mem._snapshots["s1"]
    mem._snapshots["s1"] = (text_, used - mem.notes_refresh_after)  # idle past it
    second = await mem.setup(_ctx())
    assert "new fact" not in (first.instructions or "")
    assert "new fact" in (second.instructions or "")


def _outlast_window(mem: Memory) -> Tool:
    """A tool standing in for a run that outlasts the refresh window, during
    which another session's setup sweeps this session's snapshot away."""

    @tool
    async def slow_work() -> str:
        """Do slow work."""
        text_, used = mem._snapshots["s1"]
        mem._snapshots["s1"] = (text_, used - mem.notes_refresh_after - 50)
        await mem._session_notes("other")  # the sweep
        assert "s1" not in mem._snapshots
        return "done"

    return slow_work


async def test_run_end_repins_and_restarts_the_idle_clock(tmp_path) -> None:
    # A long run keeps the provider cache warm until its last call, so the
    # window counts from run end — and survives an eviction mid-run.
    mem = Memory(tmp_path / "mem", index=None)
    provider = ScriptedProvider(
        [call("slow_work", {}, call_id="c1"), text("ok"), text("hi")]
    )
    agent = Agent(name="a", model=provider, tools=[_outlast_window(mem)], plugins=[mem])
    session = InMemorySession()
    await Runner.run(agent, "work", session=session, session_id="s1")
    await mem.remember("written after the run")
    await Runner.run(agent, "again", session=session, session_id="s1")
    assert provider.calls[2][0].content == provider.calls[0][0].content


async def test_failed_run_still_restarts_the_idle_clock(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    session = InMemorySession()
    # The script runs out after the tool call, so the run fails mid-way.
    failing = ScriptedProvider([call("slow_work", {}, call_id="c1")])
    agent = Agent(name="a", model=failing, tools=[_outlast_window(mem)], plugins=[mem])
    with pytest.raises(AssertionError, match="ran out"):
        await Runner.run(agent, "work", session=session, session_id="s1")
    await mem.remember("written after the run")
    retry = ScriptedProvider([text("hi")])
    await Runner.run(
        agent.clone(model=retry), "again", session=session, session_id="s1"
    )
    assert retry.calls[0][0].content == failing.calls[0][0].content


async def test_edit_during_snapshot_render_is_not_pinned_over(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("old fact")
    render = mem._render

    async def render_then_edit(scope) -> str:
        stale = await render(scope)
        await mem.replace_notes("- edited by hand")  # lands mid-setup
        return stale

    mem._render = render_then_edit  # type: ignore[method-assign]
    first = await mem.setup(_ctx())
    mem._render = render  # type: ignore[method-assign]
    second = await mem.setup(_ctx())
    assert "old fact" in (first.instructions or "")
    assert "edited by hand" in (second.instructions or "")


async def test_zero_refresh_window_reads_notes_every_run(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None, notes_refresh_after=0)
    _, second = await _two_runs(mem)
    assert "user likes vim" in second
    assert mem._snapshots == {}


async def test_sessionless_runs_read_notes_live(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    _, second = await _two_runs(mem, session_id=None)
    assert "user likes vim" in second


async def test_replace_notes_unpins_every_session(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.setup(_ctx(session_id="s1"))
    await mem.setup(_ctx(session_id="s2"))
    await mem.replace_notes("- edited by hand")
    assert mem._snapshots == {}
    inst = await mem.setup(_ctx(session_id="s1"))
    assert "edited by hand" in (inst.instructions or "")


def test_negative_refresh_window_rejected(tmp_path) -> None:
    with pytest.raises(UserError, match="notes_refresh_after"):
        Memory(tmp_path / "mem", notes_refresh_after=-1)


# ---------------------------------------------------------------------------
# Tools through a scripted run
# ---------------------------------------------------------------------------


async def test_system_prompt_carries_notes(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("the sky is blue")
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
    mem = Memory(tmp_path / "mem", index=None)
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "remember that I like vim")
    assert "user likes vim" in _bare(await mem._user.notes.load())
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("Remembered" in r.output for r in tool_results)


async def test_remember_tool_reports_duplicates(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("remember", {"fact": "user likes vim"}, call_id="c1"), text("ok")]
    )
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("user likes vim")
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "again")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("Already in your notes" in r.output for r in tool_results)


async def _remember_via_run(mem: Memory, **args: str) -> str:
    """Call the `remember` tool once through a run; return what it answered."""
    provider = ScriptedProvider([call("remember", args, call_id="c1"), text("ok")])
    result = await Runner.run(Agent(name="a", model=provider, plugins=[mem]), "go")
    return next(e.output for e in result.entries if e.type == "tool_result")


async def test_remember_tool_replaces_and_restamps(tmp_path, monkeypatch) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.replace_notes("- [2025-01] user prefers pip")
    monkeypatch.setattr(plugin_mod, "_current_month", lambda: "2026-01")
    # The model echoes the stamp it saw; today's fact is dated today.
    out = await _remember_via_run(
        mem, fact="[2025-01] user prefers uv", replaces="[2025-01] user prefers pip"
    )
    assert "Replaced" in out
    assert await mem._user.notes.load() == ["[2026-01] user prefers uv"]


async def test_remember_tool_reports_an_unmatched_replaces(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("likes jazz")
    out = await _remember_via_run(mem, fact="uses uv", replaces="uses conda")
    assert "No note matches" in out
    assert _bare(await mem._user.notes.load()) == ["likes jazz"]


async def test_remember_tool_refuses_when_notes_are_full(tmp_path) -> None:
    mem = Memory(tmp_path / "mem", index=None, notes_budget=40)
    await mem.remember("the user lives in Lisbon")
    out = await _remember_via_run(mem, fact="the user keeps two cats")
    # Refused, with the layer's current notes so the model can make room.
    assert "full" in out and "the user lives in Lisbon" in out
    assert _bare(await mem._user.notes.load()) == ["the user lives in Lisbon"]
    # Replacing is how room gets made.
    out = await _remember_via_run(
        mem, fact="the user lives in Porto", replaces="the user lives in Lisbon"
    )
    assert "Replaced" in out
    assert _bare(await mem._user.notes.load()) == ["the user lives in Porto"]


async def test_over_budget_notes_can_still_shrink(tmp_path) -> None:
    # Hand-edited past the budget: a replacement that doesn't grow the list
    # goes through, so the model is never locked out of making room.
    mem = Memory(tmp_path / "mem", index=None, notes_budget=20)
    await mem.replace_notes("- a note well past the tiny budget\n- another one")
    out = await _remember_via_run(
        mem, fact="short", replaces="a note well past the tiny budget"
    )
    assert "Replaced" in out
    assert _bare(await mem._user.notes.load()) == ["another one", "short"]


async def test_forget_tool_via_run(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("forget", {"fact": "user likes vim"}, call_id="c1"), text("Done")]
    )
    mem = Memory(tmp_path / "mem", index=None)
    await mem.remember("user likes vim")
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "forget that I like vim")
    assert await mem._user.notes.load() == []


async def test_forget_tool_reports_no_match(tmp_path) -> None:
    provider = ScriptedProvider(
        [call("forget", {"fact": "nothing like this"}, call_id="c1"), text("ok")]
    )
    mem = Memory(tmp_path / "mem", index=None)
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
    mem = Memory(tmp_path / "mem", index=index)
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
    mem = Memory(tmp_path / "mem", index=index)
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
    mem = Memory(tmp_path / "mem", index=index, summarize_recall=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "?")
    assert calls["n"] == 0
    assert index.queries == ["q"]


# ---------------------------------------------------------------------------
# End of run: the run is indexed, and nothing else happens
# ---------------------------------------------------------------------------


async def test_run_completed_indexes_the_run_as_one_doc(tmp_path) -> None:
    index = SpyIndex()
    mem = Memory(tmp_path / "mem", index=index)
    provider = ScriptedProvider([text("Arr, hello matey!")])
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "ahoy there sailor", session_id="s1")

    (doc,) = index.added
    assert doc.text == "USER: ahoy there sailor\nASSISTANT: Arr, hello matey!"
    assert doc.meta == {"run_id": doc.id, "session_id": "s1"}
    assert doc.when > 0
    # No model call beyond the run's own, and Notes are the agent's to write.
    assert len(provider.calls) == 1
    assert await mem._user.notes.load() == []


async def test_ingest_failure_never_fails_the_run(tmp_path) -> None:
    class BrokenIndex(SpyIndex):
        async def add(self, docs: list[Doc]) -> None:
            raise RuntimeError("index exploded")

    mem = Memory(tmp_path / "mem", index=BrokenIndex())
    agent = Agent(name="a", model=ScriptedProvider([text("ok")]), plugins=[mem])
    assert (await Runner.run(agent, "hello")).output == "ok"


async def test_a_supersession_is_one_save(tmp_path) -> None:
    # Old note out, new fact in, in a single save: no reader can observe the
    # in-between state (old gone, new missing).
    class SpyNotes:
        def __init__(self) -> None:
            self.facts: list[str] = ["[2026-01] user prefers pip"]
            self.saves = 0

        async def load(self) -> list[str]:
            return list(self.facts)

        async def save(self, facts: list[str]) -> None:
            self.saves += 1
            self.facts = list(facts)

    notes = SpyNotes()
    mem = Memory(tmp_path / "mem", notes=notes, index=None)
    assert await mem.remember("user prefers uv", replaces="user prefers pip")
    assert notes.saves == 1
    assert _bare(notes.facts) == ["user prefers uv"]


async def test_recall_uses_the_model_override(tmp_path, monkeypatch) -> None:
    seen = {}

    async def fake_summarize(hits, query, model):
        seen["model"] = model
        return "summary"

    monkeypatch.setattr(plugin_mod, "_summarize", fake_summarize)
    index = SpyIndex([Hit(doc=Doc(id="d1", text="my dog Rex"), score=1.0)])
    mem = Memory(tmp_path / "mem", index=index, model="openai:some-cheap-model")
    provider = ScriptedProvider([call("recall", {"query": "dog"}), text("ok")])
    await Runner.run(Agent(name="a", model=provider, plugins=[mem]), "dog?")
    assert seen["model"] == "openai:some-cheap-model"


# ---------------------------------------------------------------------------
# One doc per run, keyed by the run id → idempotent re-ingest
# ---------------------------------------------------------------------------


def test_run_doc_id_is_the_run_id() -> None:
    entries = _msgs(("user", "hello"), ("assistant", "hi there"))
    doc = _run_doc(entries, _ctx(run_id="r1"))
    assert doc is not None
    assert (doc.id, doc.meta) == ("r1", {"run_id": "r1", "session_id": "s1"})
    # No session_id → no meta key.
    other = _run_doc(entries, _ctx(run_id="r2", session_id=None))
    assert other is not None
    assert (other.id, other.meta) == ("r2", {"run_id": "r2"})


async def test_reingest_upserts_instead_of_duplicating(tmp_path) -> None:
    # A resumed run is indexed again under the same run id: an upsert.
    index = KeywordIndex(":memory:")
    mem = Memory(tmp_path / "mem", index=index)
    entries = _msgs(("user", "zebra fact one"), ("assistant", "noted zebra"))
    await mem._archive(mem._user, entries, _ctx(run_id="r1"))
    await mem._archive(mem._user, entries, _ctx(run_id="r1"))
    assert len(await index.search("zebra", k=10)) == 1


def test_run_doc_skips_tool_and_system_entries() -> None:
    entries = [
        InputEntry(role="system", content="system prompt should not be archived"),
        InputEntry(role="user", content="user question echotoken"),
        ToolCallEntry(call_id="c1", name="foo", arguments="{}"),
        ToolResultEntry(call_id="c1", output="tool output should not be archived"),
        AssistantTextEntry(content="assistant answer echotoken"),
    ]
    doc = _run_doc(entries, _ctx())
    assert doc is not None
    assert doc.text == (
        "USER: user question echotoken\nASSISTANT: assistant answer echotoken"
    )
    # A run that said nothing leaves no doc.
    assert _run_doc(entries[2:4], _ctx()) is None


def test_long_run_doc_keeps_its_head_and_tail() -> None:
    entries = _msgs(
        ("user", "HEAD " + "x" * 20000), ("assistant", "y" * 20000 + " TAIL")
    )
    doc = _run_doc(entries, _ctx())
    assert doc is not None
    assert doc.text.startswith("USER: HEAD") and doc.text.endswith(" TAIL")
    assert "\n[...]\n" in doc.text
    assert len(doc.text) < 8100


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
    return os.getenv("LOVIA_LIVE_OPENAI_MODEL", "gpt-5.5")


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

    # Run 1: state a durable preference (the agent should remember it).
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
        expand_query=True,
    )
    await mem._archive(
        mem._user,
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
