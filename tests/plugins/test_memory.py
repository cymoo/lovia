"""Tests for the Memory plugin: Notes (hot) + Archive (cold), tools, hooks.

The unit tests are network-free: they drive the runner with the scripted
provider and monkeypatch the LLM curation side-queries. The opt-in live e2e
tests exercise the real provider configured in ``.env``::

    LOVIA_LIVE_TESTS=1 uv run pytest tests/test_memory.py -k live
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from lovia import Agent, ModelSettings, Runner
from lovia.plugins import memory as memory_mod
from lovia.plugins.memory import (
    FileNotesStore,
    Memory,
    SQLiteArchiveStore,
    _drop_fact,
    _format_facts,
    _fts5_available,
    _meter,
    _parse_facts,
    _terms,
)
from lovia.transcript import (
    AssistantTextEntry,
    InputEntry,
    ToolCallEntry,
    ToolResultEntry,
)

from ..scripted_provider import ScriptedProvider, call, text

requires_fts = pytest.mark.skipif(
    not _fts5_available(), reason="SQLite built without FTS5"
)


def _msgs(*pairs: tuple[str, str]) -> list:
    """Build a simple user/assistant transcript from (role, content) pairs."""
    entries: list = []
    for role, content in pairs:
        if role == "user":
            entries.append(InputEntry(role="user", content=content))
        else:
            entries.append(AssistantTextEntry(content=content))
    return entries


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


def test_drop_fact_strategies() -> None:
    assert _drop_fact(["a", "b"], "a") == ["b"]
    assert _drop_fact(["Apple"], "apple") == []  # case-insensitive
    assert _drop_fact(["I like apples a lot"], "apples") == []  # substring
    assert _drop_fact(["x"], "nope") == ["x"]  # no match → unchanged


# ---------------------------------------------------------------------------
# FileNotesStore (hot tier)
# ---------------------------------------------------------------------------


async def test_notes_add_dedup_and_normalize(tmp_path) -> None:
    store = FileNotesStore(tmp_path / "MEMORY.md")
    await store.add("I prefer tabs over spaces.")
    await store.add("  I prefer   tabs over spaces.  ")  # whitespace-normalized dup
    await store.add("I PREFER TABS OVER SPACES.")  # case-insensitive dup
    await store.add("My name is\nAlice")  # multi-line → single line
    await store.add("   ")  # blank → ignored
    assert _parse_facts(await store.raw()) == [
        "I prefer tabs over spaces.",
        "My name is Alice",
    ]


async def test_notes_remove_fuzzy(tmp_path) -> None:
    store = FileNotesStore(tmp_path / "MEMORY.md")
    await store.add("I prefer tabs over spaces.")
    await store.add("My name is Alice")
    await store.remove("my name is alice")  # case-insensitive
    assert _parse_facts(await store.raw()) == ["I prefer tabs over spaces."]
    await store.remove("tabs")  # substring
    assert await store.raw() == ""
    await store.remove("nothing here")  # no-op, no raise


async def test_notes_render_meter_and_empty(tmp_path) -> None:
    store = FileNotesStore(tmp_path / "MEMORY.md", max_chars=100)
    empty = await store.render()
    assert "NOTES [0% — 0/100 chars]" in empty
    assert "(empty" in empty
    await store.add("hello world")  # body "- hello world" == 13 chars
    rendered = await store.render()
    assert "- hello world" in rendered
    assert "13/100 chars]" in rendered


async def test_notes_replace_normalizes(tmp_path) -> None:
    store = FileNotesStore(tmp_path / "MEMORY.md")
    await store.add("old")
    await store.replace("- new one\n- new two")
    assert _parse_facts(await store.raw()) == ["new one", "new two"]


async def test_notes_concurrent_writes_are_serialized(tmp_path) -> None:
    store = FileNotesStore(tmp_path / "MEMORY.md")
    await asyncio.gather(*(store.add(f"fact number {i}") for i in range(50)))
    facts = _parse_facts(await store.raw())
    assert len(facts) == 50
    assert set(facts) == {f"fact number {i}" for i in range(50)}


# ---------------------------------------------------------------------------
# SQLiteArchiveStore (cold tier)
# ---------------------------------------------------------------------------


@requires_fts
async def test_archive_fts_ranking_and_filtering() -> None:
    arc = SQLiteArchiveStore(":memory:")
    assert arc._use_fts
    await arc.ingest(
        "s1",
        _msgs(
            ("user", "I love hiking in the mountains"),
            ("assistant", "Mountains are wonderful for a hiking trip"),
        ),
    )
    await arc.ingest(
        "s2",
        _msgs(
            ("user", "best pasta recipe please"),
            ("assistant", "carbonara with guanciale"),
        ),
    )
    hits = await arc.search("hiking mountains", k=5)
    assert hits
    # Only the hiking session matches these tokens.
    assert all(h.session_id == "s1" for h in hits)
    assert all(
        "hik" in h.text.lower() or "mountain" in h.text.lower() for h in hits
    )
    # bm25-ranked: we report -bm25, so higher score == better; results best-first.
    assert hits == sorted(hits, key=lambda h: h.score, reverse=True)


@requires_fts
async def test_archive_cjk_search() -> None:
    arc = SQLiteArchiveStore(":memory:")
    assert arc._use_fts
    await arc.ingest(
        "s1",
        _msgs(
            ("user", "我今天去了北京出差，顺便看了朋友"),
            ("assistant", "北京很好玩，我爱 python"),
        ),
    )
    # Two-char CJK words match: the default unicode61 tokenizer keeps a whole CJK
    # run as one token and misses these; the bigram index segments them.
    assert await arc.search("北京")
    assert await arc.search("出差")
    # A natural-language CJK query matches via its bigrams, not just exact words.
    assert await arc.search("我想知道北京出差的情况")
    # Mixed CJK + ASCII: the ASCII word is found too.
    assert await arc.search("python")
    # A word that never appears does not match.
    assert await arc.search("广州") == []
    # bm25 ranks the message with more matching bigrams first.
    hits = await arc.search("北京出差")
    assert hits[0].text == "我今天去了北京出差，顺便看了朋友"


async def test_archive_like_fallback(monkeypatch) -> None:
    monkeypatch.setattr(memory_mod, "_fts5_available", lambda: False)
    arc = SQLiteArchiveStore(":memory:")
    assert not arc._use_fts
    await arc.ingest(
        "s1",
        _msgs(("user", "I love hiking in the mountains"), ("assistant", "great")),
    )
    hits = await arc.search("hiking", k=5)
    assert hits and "hiking" in hits[0].text.lower()
    assert await arc.search("   ") == []  # empty query → no hits, no error


async def test_archive_cjk_like_fallback(monkeypatch) -> None:
    monkeypatch.setattr(memory_mod, "_fts5_available", lambda: False)
    arc = SQLiteArchiveStore(":memory:")
    assert not arc._use_fts
    await arc.ingest("s1", _msgs(("user", "我今天去了北京出差"), ("assistant", "ok")))
    # The LIKE fallback segments CJK queries into bigrams too.
    assert await arc.search("北京")
    assert await arc.search("我想知道北京出差的情况")
    assert await arc.search("广州") == []


def test_terms_segmentation() -> None:
    assert _terms("hiking Mountains") == ["hiking", "mountains"]
    assert _terms("北京") == ["北京"]
    assert _terms("北京出差") == ["北京", "京出", "出差"]
    assert _terms("我爱python") == ["我爱", "python"]
    assert _terms("café Müller") == ["café", "müller"]  # accented Latin: whole
    assert _terms("Москва") == ["москва"]  # Cyrillic: whole, not bigrammed
    assert _terms("中") == ["中"]
    assert _terms("") == []
    assert _terms("!!! ???") == []


async def test_archive_empty_query_returns_nothing() -> None:
    arc = SQLiteArchiveStore(":memory:")
    await arc.ingest("s1", _msgs(("user", "hello"), ("assistant", "hi")))
    assert await arc.search("") == []
    assert await arc.search("!!! ??? ...") == []


async def test_archive_replace_on_run_is_idempotent() -> None:
    arc = SQLiteArchiveStore(":memory:")
    # Re-ingesting the SAME run_id replaces its rows (an idempotent resume),
    # rather than appending a second copy.
    await arc.ingest("s1", _msgs(("user", "alpha alpha"), ("assistant", "ok")), run_id="r1")
    await arc.ingest("s1", _msgs(("user", "bravo bravo"), ("assistant", "ok")), run_id="r1")
    assert await arc.search("alpha") == []
    assert await arc.search("bravo")


async def test_archive_runs_accumulate_across_session() -> None:
    arc = SQLiteArchiveStore(":memory:")
    # Distinct runs in one session each append their own messages — the archive
    # is the union across runs, not just the latest run.
    await arc.ingest("s1", _msgs(("user", "alpha alpha"), ("assistant", "ok")), run_id="r1")
    await arc.ingest("s1", _msgs(("user", "bravo bravo"), ("assistant", "ok")), run_id="r2")
    assert await arc.search("alpha")  # earlier run is still archived
    assert await arc.search("bravo")


@requires_fts
async def test_archive_rebuilds_stale_schema(tmp_path) -> None:
    # An on-disk archive.db from an older schema (no run_id column) must be
    # rebuilt rather than left to crash ingest. Old rows are discarded — we do
    # not migrate archived data across schema changes.
    path = tmp_path / "archive.db"
    con = sqlite3.connect(str(path))
    con.executescript(
        "CREATE VIRTUAL TABLE archive_fts USING fts5("
        "session_id UNINDEXED, text, when_ts UNINDEXED);"
    )
    con.execute("INSERT INTO archive_fts VALUES ('old', 'stale relic', 0)")
    con.commit()
    con.close()

    arc = SQLiteArchiveStore(str(path))  # detects the old schema -> recreates
    await arc.ingest("s1", _msgs(("user", "fresh hiking trip"), ("assistant", "ok")))
    assert await arc.search("hiking")  # new ingest + search work, no error
    assert await arc.search("relic") == []  # old rows discarded, not migrated


async def test_archive_oneshot_runs_all_retained() -> None:
    arc = SQLiteArchiveStore(":memory:")
    await arc.ingest(None, _msgs(("user", "charlie charlie"), ("assistant", "ok")))
    await arc.ingest(None, _msgs(("user", "delta delta"), ("assistant", "ok")))
    # No session_id → unique key per run, so both are retained.
    assert await arc.search("charlie")
    assert await arc.search("delta")


async def test_archive_skips_tool_and_system_entries() -> None:
    arc = SQLiteArchiveStore(":memory:")
    entries = [
        InputEntry(role="system", content="system prompt should not be archived"),
        InputEntry(role="user", content="user question echotoken"),
        ToolCallEntry(call_id="c1", name="foo", arguments="{}"),
        ToolResultEntry(call_id="c1", output="tool output should not be archived"),
        AssistantTextEntry(content="assistant answer echotoken"),
    ]
    await arc.ingest("s1", entries)
    assert await arc.search("system prompt") == []
    assert await arc.search("tool output") == []
    hits = await arc.search("echotoken")
    assert len(hits) == 2  # only the user + assistant messages


async def test_archive_ingest_empty_is_noop() -> None:
    arc = SQLiteArchiveStore(":memory:")
    await arc.ingest("s1", [])  # nothing to store
    assert await arc.search("anything") == []


async def test_archive_roundtrip_on_disk(tmp_path) -> None:
    # Constructing under a missing dir must not fail (parent is created).
    path = tmp_path / "nested" / "archive.db"
    arc = SQLiteArchiveStore(str(path))
    await arc.ingest("s1", _msgs(("user", "persistent zebra fact"), ("assistant", "ok")))
    assert path.exists()
    hits = await arc.search("zebra")
    assert hits and hits[0].session_id == "s1"


# ---------------------------------------------------------------------------
# Plugin construction & setup() contributions
# ---------------------------------------------------------------------------


def test_memory_path_form_builds_default_stores(tmp_path) -> None:
    mem = Memory(str(tmp_path / "mem"))
    assert isinstance(mem.notes, FileNotesStore)
    assert isinstance(mem.archive, SQLiteArchiveStore)


def test_memory_path_form_archive_none_disables_cold_tier(tmp_path) -> None:
    # Explicit archive=None must disable the cold tier even with a path notes
    # root (it would otherwise be overridden by the default archive).
    mem = Memory(str(tmp_path / "mem"), archive=None)
    assert isinstance(mem.notes, FileNotesStore)
    assert mem.archive is None


def test_memory_custom_notes_no_archive(tmp_path) -> None:
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    mem = Memory(notes=notes, archive=None)
    assert mem.notes is notes
    assert mem.archive is None


async def test_setup_instructions_include_notes_and_tools(tmp_path) -> None:
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    await notes.add("user prefers dark mode")
    mem = Memory(notes=notes, archive=SQLiteArchiveStore(":memory:"))
    inst = await mem.setup()
    assert {t.name for t in inst.tools} == {"remember", "forget", "recall"}
    assert "NOTES" in inst.instructions
    assert "user prefers dark mode" in inst.instructions
    assert "recall" in inst.instructions
    # The RunCompleted hook reads session_id straight off the injected
    # RunContext, so no capture view-injector is needed anymore.
    assert inst.view_injectors == []
    assert inst.hooks is not None


async def test_setup_no_archive(tmp_path) -> None:
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    await notes.add("durable note")
    mem = Memory(notes=notes, archive=None)
    inst = await mem.setup()
    assert {t.name for t in inst.tools} == {"remember", "forget"}  # no recall
    assert "durable note" in inst.instructions  # notes are always injected
    assert "remember" in inst.instructions  # usage guidance still present
    assert "recall" not in inst.instructions  # no archive guidance


# ---------------------------------------------------------------------------
# Tools through a scripted run
# ---------------------------------------------------------------------------


async def test_system_prompt_carries_notes(tmp_path) -> None:
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    await notes.add("the sky is blue")
    provider = ScriptedProvider([text("hi")])
    mem = Memory(notes=notes, archive=None, auto_extract=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "go")
    system = provider.calls[0][0]
    assert system.role == "system"
    assert "the sky is blue" in (system.content or "")
    assert "remember" in (system.content or "")


async def test_remember_tool_persists_via_run(tmp_path) -> None:
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    provider = ScriptedProvider(
        [call("remember", {"fact": "user likes vim"}, call_id="c1"), text("Saved!")]
    )
    mem = Memory(notes=notes, archive=None, auto_extract=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "remember that I like vim")
    assert "user likes vim" in await notes.raw()
    # The tool reported success in the transcript.
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("Remembered" in r.output for r in tool_results)


async def test_forget_tool_via_run(tmp_path) -> None:
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    await notes.add("user likes vim")
    provider = ScriptedProvider(
        [call("forget", {"fact": "user likes vim"}, call_id="c1"), text("Done")]
    )
    mem = Memory(notes=notes, archive=None, auto_extract=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "forget that I like vim")
    assert await notes.raw() == ""


async def test_recall_tool_returns_hits_without_summary(tmp_path) -> None:
    archive = SQLiteArchiveStore(":memory:")
    await archive.ingest(
        "old",
        _msgs(("user", "my dog's name is Rex"), ("assistant", "Nice, Rex!")),
    )
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    provider = ScriptedProvider(
        [call("recall", {"query": "dog name Rex"}, call_id="c1"), text("Your dog is Rex.")]
    )
    mem = Memory(
        notes=notes, archive=archive, auto_extract=False, summarize_recall=False
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

    monkeypatch.setattr(memory_mod, "_summarize", fake_summarize)
    archive = SQLiteArchiveStore(":memory:")
    await archive.ingest("old", _msgs(("user", "my dog Rex"), ("assistant", "ok")))
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    provider = ScriptedProvider(
        [call("recall", {"query": "dog"}, call_id="c1"), text("done")]
    )
    mem = Memory(
        notes=notes, archive=archive, auto_extract=False, summarize_recall=True
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "dog?")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("SUMMARY: your dog is Rex" in r.output for r in tool_results)
    assert seen["query"] == "dog"
    assert seen["n_hits"] >= 1


async def test_recall_tool_handles_no_hits(tmp_path) -> None:
    archive = SQLiteArchiveStore(":memory:")
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    provider = ScriptedProvider(
        [call("recall", {"query": "nonexistent"}, call_id="c1"), text("nothing")]
    )
    mem = Memory(
        notes=notes, archive=archive, auto_extract=False, summarize_recall=False
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    result = await Runner.run(agent, "?")
    tool_results = [e for e in result.entries if e.type == "tool_result"]
    assert any("nothing relevant" in r.output for r in tool_results)


# ---------------------------------------------------------------------------
# Hooks: archive ingest + Notes curation on RunCompleted
# ---------------------------------------------------------------------------


async def test_run_completed_ingests_and_extracts(tmp_path, monkeypatch) -> None:
    captured = {}

    async def fake_extract(entries, current, model):
        captured["called"] = True
        captured["current"] = current
        return ["the user is a pirate"]

    monkeypatch.setattr(memory_mod, "_extract", fake_extract)

    notes = FileNotesStore(tmp_path / "MEMORY.md")
    archive = SQLiteArchiveStore(":memory:")
    provider = ScriptedProvider([text("Arr, hello matey!")])
    mem = Memory(notes=notes, archive=archive, auto_extract=True)
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "ahoy there sailor", session_id="s1")

    # Extraction promoted the fact into Notes (the empty current was passed in).
    assert "the user is a pirate" in await notes.raw()
    assert captured["called"] is True
    assert captured["current"] == ""
    # The conversation was archived, keyed by session_id.
    hits = await archive.search("ahoy sailor")
    assert hits
    assert hits[0].session_id == "s1"


async def test_auto_extract_false_skips_extraction(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    async def fake_extract(*a, **k):
        calls["n"] += 1
        return ["should not appear"]

    monkeypatch.setattr(memory_mod, "_extract", fake_extract)

    notes = FileNotesStore(tmp_path / "MEMORY.md")
    archive = SQLiteArchiveStore(":memory:")
    provider = ScriptedProvider([text("hi")])
    mem = Memory(notes=notes, archive=archive, auto_extract=False)
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "hello world uniquexyz", session_id="s1")

    assert calls["n"] == 0
    assert await notes.raw() == ""
    # Archive ingest happens regardless of auto_extract.
    assert await archive.search("uniquexyz")


async def test_consolidation_triggers_over_budget(tmp_path, monkeypatch) -> None:
    async def fake_extract(entries, current, model):
        return ["a very long fact that blows the tiny budget wide open"]

    async def fake_consolidate(body, max_chars, model):
        assert len(body) > max_chars
        return ["compact fact"]

    monkeypatch.setattr(memory_mod, "_extract", fake_extract)
    monkeypatch.setattr(memory_mod, "_consolidate", fake_consolidate)

    notes = FileNotesStore(tmp_path / "MEMORY.md", max_chars=20)
    provider = ScriptedProvider([text("ok")])
    mem = Memory(notes=notes, archive=None, auto_extract=True)
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "go")

    assert _parse_facts(await notes.raw()) == ["compact fact"]


async def test_extraction_failure_does_not_break_run(tmp_path, monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr(memory_mod, "_extract", boom)
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    archive = SQLiteArchiveStore(":memory:")
    provider = ScriptedProvider([text("still fine")])
    mem = Memory(notes=notes, archive=archive, auto_extract=True)
    agent = Agent(name="a", model=provider, plugins=[mem])
    # The hook swallows extractor errors; the run still completes.
    result = await Runner.run(agent, "hello", session_id="s1")
    assert result.output == "still fine"
    assert await notes.raw() == ""
    assert await archive.search("hello")  # ingest still happened


async def test_model_override_used_for_curation(tmp_path, monkeypatch) -> None:
    seen = {}

    async def fake_extract(entries, current, model):
        seen["model"] = model
        return []

    monkeypatch.setattr(memory_mod, "_extract", fake_extract)
    notes = FileNotesStore(tmp_path / "MEMORY.md")
    provider = ScriptedProvider([text("hi")])
    mem = Memory(
        notes=notes, archive=None, auto_extract=True, model="openai:some-cheap-model"
    )
    agent = Agent(name="a", model=provider, plugins=[mem])
    await Runner.run(agent, "go")
    assert seen["model"] == "openai:some-cheap-model"


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
    _load_env_file()
    if os.getenv("LOVIA_LIVE_TESTS") != "1":
        pytest.skip("opt-in: set LOVIA_LIVE_TESTS=1 to run live provider tests")
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not configured")
    return os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5.4")


@pytest.mark.live_provider
async def test_live_notes_persist_across_sessions(tmp_path) -> None:
    model = _live_model()
    notes_path = tmp_path / "MEMORY.md"
    archive_path = tmp_path / "archive.db"

    def make_agent() -> Agent:
        mem = Memory(
            notes=FileNotesStore(notes_path),
            archive=SQLiteArchiveStore(str(archive_path)),
        )
        return Agent(
            name="assistant",
            model=f"openai:{model}",
            instructions="You are concise and helpful.",
            settings=ModelSettings(temperature=0),
            plugins=[mem],
        )

    # Run 1: state a durable preference (auto_extract should promote it).
    await Runner.run(
        make_agent(),
        "Please remember that I strongly prefer Python over JavaScript for all "
        "code examples you give me.",
        session_id="live-pref-1",
    )
    body = notes_path.read_text().lower()
    assert "python" in body, f"preference not promoted into Notes; got: {body!r}"

    # Run 2: a fresh agent over the same Notes file should already know it.
    res2 = await Runner.run(
        make_agent(),
        "What programming language will you use for code examples for me, and why?",
        session_id="live-pref-2",
    )
    assert "python" in str(res2.output).lower()


@pytest.mark.live_provider
async def test_live_archive_recall(tmp_path) -> None:
    model = _live_model()
    archive = SQLiteArchiveStore(str(tmp_path / "archive.db"))
    await archive.ingest(
        "old-trip",
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
    )
    mem = Memory(
        notes=FileNotesStore(tmp_path / "MEMORY.md"),
        archive=archive,
        auto_extract=False,
        summarize_recall=True,
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
