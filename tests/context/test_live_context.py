"""Opt-in live tests for the context pipeline against a real model endpoint.

These exercise the full production path — real LLM summarization, fact
retention across compaction, mid-run clearing bursts, recall, offload, and a
large-history run — using the OpenAI-compatible endpoint configured in
``.env``. Run with::

    LOVIA_LIVE_TESTS=1 uv run pytest tests/context/test_live_context.py -q

The genuine context-overflow probe additionally requires
``LOVIA_LIVE_OVERFLOW_TESTS=1`` (it deliberately sends an oversized prompt).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lovia import (
    Agent,
    AssistantTextEntry,
    ContextOverflowError,
    Compaction,
    InMemorySession,
    InputEntry,
    ModelSettings,
    NoopContextPolicy,
    Runner,
    events,
    provider_from_string,
)
from lovia.context import (
    REQUIRED_SECTIONS,
    ClearToolResults,
    CompactionRequest,
    LLMSummarizer,
    OffloadToolResults,
    SummarizeHistory,
)
from lovia.tools import recall_tool_result, tool
from lovia.transcript import ToolCallEntry, ToolResultEntry
from lovia.workspace import Workspace

pytestmark = pytest.mark.live_provider


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


def _agent(model_name: str, instructions: str, **kw) -> Agent:
    return Agent(
        name="ctx-probe",
        model=f"openai:{model_name}",
        instructions=instructions,
        settings=ModelSettings(temperature=0),
        **kw,
    )


def _user(s: str) -> InputEntry:
    return InputEntry(role="user", content=s)


def _assistant(s: str) -> AssistantTextEntry:
    return AssistantTextEntry(content=s)


def _chat_history(pairs: int, *, facts: dict[int, str] | None = None) -> list:
    """Fabricate a long, mildly varied chat history with planted facts."""
    facts = facts or {}
    seeded: list = []
    for i in range(pairs):
        question = f"Trivia round {i}: tell me something about ocean animal #{i}."
        answer = (
            f"Fact #{i}: ocean animal #{i} migrates roughly {100 + i} kilometers "
            f"each season and feeds mostly at night. "
            f"Researchers tracking population {i} observed seasonal depth changes, "
            f"cooperative hunting, distinctive acoustic signatures, and strong "
            f"site fidelity to feeding ground number {i} across multiple years. "
            f"Tagging study {i} also recorded temperature preferences and an "
            f"average dive duration of {3 + i % 7} minutes."
        )
        if i in facts:
            question = facts[i]
            answer = "Understood, I will remember that."
        seeded.append(_user(question))
        seeded.append(_assistant(answer))
    return seeded


async def _run_collect(agent, input_text, **kw):
    handle = Runner.stream(agent, input_text, **kw)
    seen: list[events.Event] = []
    async for ev in handle:
        seen.append(ev)
    return await handle.result(), seen


def _compactions(seen: list[events.Event]) -> list[events.ContextCompacted]:
    return [e for e in seen if isinstance(e, events.ContextCompacted)]


# ---------------------------------------------------------------------------
# 1. Proactive summary burst preserves planted facts
# ---------------------------------------------------------------------------


async def test_live_summary_burst_preserves_key_fact():
    model = _live_model()
    fact = "Important: our secret project codename is ZANZIBAR-7. Never forget it."
    sess = InMemorySession()
    await sess.append("s1", _chat_history(40, facts={5: fact, 12: fact}))

    agent = _agent(model, "You are a concise assistant.")
    policy = Compaction(
        context_window=4_000,
        reserve_output_tokens=1_000,
        compact_at=0.5,
        compact_to=0.3,
    )
    result, seen = await _run_collect(
        agent,
        "What is our secret project codename? Reply with just the codename.",
        context_policy=policy,
        session=sess,
        session_id="s1",
    )

    compacted = _compactions(seen)
    assert compacted, "expected at least one compaction burst"
    assert any("summary" in e.reason for e in compacted)
    assert any(e.summary for e in compacted)
    # The planted identifier survived real LLM summarization.
    assert "zanzibar" in (result.output or "").lower()
    # The session was never polluted by the summary.
    persisted = await sess.load("s1")
    assert not any(
        "<context_summary>" in str(getattr(e, "content", "")) for e in persisted
    )


# ---------------------------------------------------------------------------
# 2. LLMSummarizer emits the structured sections and folds incrementally
# ---------------------------------------------------------------------------


async def test_live_summarizer_sections_and_incremental_fold():
    model = _live_model()
    provider = provider_from_string(f"openai:{model}")
    try:
        summarizer = LLMSummarizer(provider)
        req = CompactionRequest(entries=[])
        first_span = [
            _user(
                "Help me migrate the billing service to Postgres 17. "
                "Constraint: zero downtime, and never touch the audit tables."
            ),
            _assistant(
                "Plan: 1) provision replica, 2) run pgloader, 3) switch DNS. "
                "I created scripts/migrate.sh and updated config/db.yaml."
            ),
            _user("Good. The maintenance window is Saturday 02:00 UTC."),
        ]
        summary = await summarizer.summarize(first_span, req=req)
        lowered = summary.lower()
        for section in REQUIRED_SECTIONS:
            assert section.lower() in lowered, f"missing {section!r} in:\n{summary}"
        assert "postgres 17" in lowered
        assert "migrate.sh" in lowered

        folded = await summarizer.summarize(
            [
                _user("Update: the rollback budget is 15 minutes, code ROLLBK-15."),
                _assistant("Noted. I added the rollback step to scripts/migrate.sh."),
            ],
            req=req,
            prior_summary=summary,
        )
        folded_lower = folded.lower()
        for section in REQUIRED_SECTIONS:
            assert section.lower() in folded_lower
        assert "rollbk-15" in folded_lower  # new fact folded in
        assert "postgres 17" in folded_lower  # old fact retained
    finally:
        aclose = getattr(provider, "aclose", None)
        if callable(aclose):
            await aclose()


# ---------------------------------------------------------------------------
# 3. Mid-run clearing burst: old tool results dropped, recent kept usable
# ---------------------------------------------------------------------------


async def test_live_tool_clearing_burst_mid_run():
    model = _live_model()

    @tool
    def lookup(page: int) -> str:
        """Fetch one page of the report archive."""
        filler = f"archived report data chunk for page {page}; " * 150
        return filler + f"\n[page {page} verification token: TOKEN-{page}-OK]"

    agent = _agent(
        model,
        "Call the lookup tool for page 1, then page 2, then page 3 — exactly "
        "one call at a time, in order. After page 3, reply with only the "
        "verification token from page 3.",
        tools=[lookup],
    )
    policy = Compaction(
        context_window=8_000,
        reserve_output_tokens=1_000,
        compact_at=0.5,
        compact_to=0.3,
        stages=[ClearToolResults(keep_last=1)],
    )
    result, seen = await _run_collect(agent, "Start now.", context_policy=policy)

    assert "TOKEN-3-OK" in (result.output or "")
    compacted = _compactions(seen)
    assert compacted and any("clear" in e.reason for e in compacted)
    # The real transcript still holds every full tool output.
    full_outputs = [
        e
        for e in result.entries
        if isinstance(e, ToolResultEntry) and "verification token" in e.output
    ]
    assert len(full_outputs) == 3


# ---------------------------------------------------------------------------
# 4. recall_tool_result closes the loop after clearing
# ---------------------------------------------------------------------------


async def test_live_recall_after_clearing():
    model = _live_model()
    filler = "irrelevant log line; " * 100
    sess = InMemorySession()
    seeded: list = []
    for i in range(1, 5):
        payload = filler + (
            "the magic word is PERSIMMON" if i == 1 else f"nothing special on {i}"
        )
        seeded.append(ToolCallEntry(call_id=f"c{i}", name="fetch", arguments="{}"))
        seeded.append(ToolResultEntry(call_id=f"c{i}", output=payload))
    await sess.append("s1", seeded)

    agent = _agent(
        model,
        "You can retrieve dropped tool outputs with recall_tool_result.",
        tools=[recall_tool_result],
    )
    policy = Compaction(
        context_window=2_500,
        reserve_output_tokens=500,
        compact_at=0.5,
        compact_to=0.3,
        stages=[ClearToolResults(keep_last=1)],
    )
    result, seen = await _run_collect(
        agent,
        'An earlier tool result (call_id "c1") contained a magic word. '
        "Retrieve it and reply with only the magic word.",
        context_policy=policy,
        session=sess,
        session_id="s1",
    )

    assert _compactions(seen), "clearing burst should have fired"
    assert "persimmon" in (result.output or "").lower()


# ---------------------------------------------------------------------------
# 5. Offload archives to a real workspace
# ---------------------------------------------------------------------------


async def test_live_offload_archives_to_workspace(tmp_path):
    model = _live_model()
    big = ("measurement row 42,17,93; " * 300) + "final checksum: CHK-7741"
    sess = InMemorySession()
    await sess.append(
        "s1",
        [
            ToolCallEntry(call_id="d1", name="export", arguments="{}"),
            ToolResultEntry(call_id="d1", output=big),
            ToolCallEntry(call_id="d2", name="export", arguments="{}"),
            ToolResultEntry(call_id="d2", output=big),
        ],
    )
    agent = _agent(
        model,
        "You are a concise assistant.",
        workspace=Workspace.local(str(tmp_path)),
    )
    policy = Compaction(
        context_window=3_000,
        reserve_output_tokens=500,
        compact_at=0.5,
        compact_to=0.25,
        stages=[
            OffloadToolResults(min_chars=2_000, keep_last=1),
            ClearToolResults(),
            SummarizeHistory(),
        ],
    )
    result, seen = await _run_collect(
        agent,
        "Say OK.",
        context_policy=policy,
        session=sess,
        session_id="s1",
    )

    assert result.output
    compacted = _compactions(seen)
    assert compacted and any("offload" in e.reason for e in compacted)
    archived = tmp_path / ".context" / "tool-d1.txt"
    assert archived.exists() and archived.read_text() == big


# ---------------------------------------------------------------------------
# 6. Large history (tens of thousands of tokens) compacts within budget
# ---------------------------------------------------------------------------


async def test_live_large_history_compacts_within_budget():
    model = _live_model()
    fact = "By the way, my favorite number is 7117 — please remember it."
    pairs = 120
    sess = InMemorySession()
    await sess.append(
        "s1",
        _chat_history(pairs, facts={pairs - 1: fact}),  # fact in the tail
    )

    agent = _agent(model, "You are a concise assistant.")
    policy = Compaction(context_window=16_000, reserve_output_tokens=2_000)
    result, seen = await _run_collect(
        agent,
        "What is my favorite number? Reply with just the number.",
        context_policy=policy,
        session=sess,
        session_id="s1",
    )

    compacted = _compactions(seen)
    assert compacted, "a history this large must trigger compaction"
    tokens_after = compacted[-1].metadata.get("tokens_after")
    assert isinstance(tokens_after, int) and tokens_after < 14_000
    # The fact sat inside the protected tail — must survive verbatim.
    assert "7117" in (result.output or "")


# ---------------------------------------------------------------------------
# 7. Genuine provider overflow is detected (separately gated: big request)
# ---------------------------------------------------------------------------


async def test_live_real_overflow_raises_context_overflow_error():
    model = _live_model()
    if os.getenv("LOVIA_LIVE_OVERFLOW_TESTS") != "1":
        pytest.skip("opt-in: set LOVIA_LIVE_OVERFLOW_TESTS=1 (sends a huge prompt)")

    # Incompressible pseudo-random words so BPE cannot shrink the prompt:
    # ~600K words ≈ well over 1M real tokens, beyond even a 1M-context model.
    import random

    rng = random.Random(42)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    sess = InMemorySession()
    for _ in range(30):
        chunk = " ".join(
            "".join(rng.choice(alphabet) for _ in range(6)) for _ in range(20_000)
        )
        await sess.append("s1", [_user(chunk)])
    agent = _agent(model, "Reply with OK.")
    with pytest.raises(ContextOverflowError):
        await Runner.run(
            agent,
            "OK?",
            context_policy=NoopContextPolicy(),
            session=sess,
            session_id="s1",
        )
