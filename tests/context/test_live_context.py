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
    ContextOverflowError,
    Compaction,
    InMemorySession,
    ModelSettings,
    Runner,
    events,
)
from lovia.providers import provider_from_string
from lovia.transcript import AssistantTextEntry, InputEntry
from lovia.context import (
    REQUIRED_SECTIONS,
    ClearToolResults,
    CompactionRequest,
    InMemoryResultStore,
    LLMSummarizer,
    NoopContextPolicy,
    OffloadToolResults,
    SummarizeHistory,
)
from lovia.context.state import result_digest
from lovia.tools import tool
from lovia.transcript import ToolCallEntry, ToolResultEntry

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
    # Gate before loading: a normal run must not pull real .env keys into
    # os.environ (and the opt-in itself must come from the shell, not .env).
    if os.getenv("LOVIA_LIVE_TESTS") != "1":
        pytest.skip("opt-in: set LOVIA_LIVE_TESTS=1 to run live provider tests")
    _load_env_file()
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not configured")
    return os.getenv("LOVIA_LIVE_OPENAI_MODEL", "gpt-5.5")


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
    assert any("summary" in e.notice.reason for e in compacted)
    assert any(e.notice.summary for e in compacted)
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


async def test_live_a_model_switch_keeps_each_models_ratio():
    """Calibrate on the real endpoint, then switch the session to a second
    endpoint identity: it starts its own ratio instead of inheriting the
    first one's, and the first one's ratio is kept for when it comes back."""
    from lovia.session import STATE_META_KEY

    model = _live_model()
    real = provider_from_string(f"openai:{model}")

    class OtherEndpoint:
        """The same real model, under a different endpoint identity."""

        name = "other"
        base_url = "https://other-endpoint.test/v1"

        def __init__(self) -> None:
            self.model = model

        async def stream(self, entries, **kw):
            async for delta in real.stream(entries, **kw):
                yield delta

    @tool
    def pick_animal() -> str:
        """Pick an animal."""
        return "otter"

    instructions = "Call pick_animal, then answer in three words."
    sess = InMemorySession()
    await sess.append("s", _chat_history(40))
    policy = Compaction(context_window=1_000_000)
    try:
        # Two calls in one run (a tool loop) calibrate within it: the
        # session's first run has no predecessor whose count it could carry.
        await Runner.run(
            _agent(model, instructions, tools=[pick_animal]),
            "Which animal?",
            context_policy=policy,
            session=sess,
            session_id="s",
        )

        async def carried() -> dict:
            meta = (await sess.segments("s"))[-1].meta or {}
            return meta[STATE_META_KEY]["context"]

        first = await carried()
        assert first["ratio_key"].startswith(real.base_url)
        assert first["ratio"] != 1.0  # calibrated on real counts

        other = OtherEndpoint()
        await Runner.run(
            Agent(
                name="o", model=other, instructions=instructions, tools=[pick_animal]
            ),
            "One more?",
            context_policy=policy,
            session=sess,
            session_id="s",
        )
        second = await carried()
        assert second["ratio_key"].startswith(other.base_url)
        assert second["ratios"][first["ratio_key"]] == first["ratio"]
    finally:
        aclose = getattr(real, "aclose", None)
        if callable(aclose):
            await aclose()


async def test_live_plain_chat_calibrates_across_runs():
    """One call per run (#507): each run carries its final call's real count
    to the next, so the ratio leaves 1.0 and the calibrated estimate of the
    last view lands close to the endpoint's real count."""
    from lovia.context import CompactionState
    from lovia.session import CALIBRATION_META_KEY, STATE_META_KEY

    model = _live_model()
    sess = InMemorySession()
    await sess.append("s", _chat_history(40))
    policy = Compaction(context_window=1_000_000)
    agent = _agent(model, "Answer in at most five words.")
    for i in range(5):
        await Runner.run(
            agent,
            f"Name one ocean animal, try #{i}.",
            context_policy=policy,
            session=sess,
            session_id="s",
        )
    segments = await sess.segments("s")
    states = [
        CompactionState.load(seg.meta[STATE_META_KEY])
        for seg in segments
        if seg.meta and STATE_META_KEY in seg.meta
    ]
    ratios = [s.ratio for s in states]
    assert ratios[0] == 1.0 and ratios[-1] != 1.0, ratios
    last = segments[-1].meta or {}
    real = last[CALIBRATION_META_KEY]
    estimate = states[-1].last_view_estimate
    assert estimate is not None
    # Calibrated estimate vs the endpoint's real count of the same view.
    error = abs(int(estimate * states[-1].ratio) - real) / real
    print(f"ratios {[round(r, 3) for r in ratios]}; final error {error:.1%}")
    assert error < abs(estimate - real) / real  # calibration helped


async def test_live_summarizer_learns_a_smaller_folding_window():
    """A summarizer on its own, smaller endpoint that publishes no window
    (#501): the first fold is refused with the limit named, the chunk is
    re-cut to fit, real summaries come back, and a planted fact survives."""
    model = _live_model()
    real = provider_from_string(f"openai:{model}")
    limit = 16_000

    class SmallEndpoint:
        """The real model, refusing prompts over ``limit`` like a small one."""

        name = "small"
        base_url = "https://small-summaries.test/v1"

        def __init__(self) -> None:
            self.model = model
            self.refusals = 0

        async def stream(self, entries, **kw):
            from lovia.context import TokenCounter

            if TokenCounter().count(entries) > limit:
                self.refusals += 1
                raise ContextOverflowError(
                    f"This model's maximum context length is {limit} tokens.",
                    reported_window=limit,
                )
            async for delta in real.stream(entries, **kw):
                yield delta

    small = SmallEndpoint()
    fact = "By the way, the vault code is VX-3308 — please remember it."
    history = _chat_history(220, facts={3: fact})  # ~26K tokens
    policy = Compaction(
        context_window=200_000,
        compact_at=0.05,
        compact_to=0.02,
        keep_recent_tokens=2_000,
        reserve_output_tokens=2_000,
        summarizer=LLMSummarizer(small),
    )
    try:
        req = CompactionRequest(
            entries=[*history, _user("What is the vault code?")],
            provider=real,
            model=model,
        )
        res = await policy.compact(req)
    finally:
        aclose = getattr(real, "aclose", None)
        if callable(aclose):
            await aclose()

    assert small.refusals == 1  # refused once, then sized to the named window
    assert res.summary is not None
    for section in REQUIRED_SECTIONS:
        assert section.lower() in res.summary.lower()
    assert "vx-3308" in res.summary.lower()


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
    assert compacted and any("clear" in e.notice.reason for e in compacted)
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

    # recall_tool_result is provided automatically by the compacting policy.
    agent = _agent(
        model,
        "You can retrieve dropped tool outputs with recall_tool_result.",
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


async def test_live_offload_archives_to_store():
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
    agent = _agent(model, "You are a concise assistant.")
    store = InMemoryResultStore()
    policy = Compaction(
        context_window=3_000,
        reserve_output_tokens=500,
        compact_at=0.5,
        compact_to=0.25,
        store=store,
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
    assert compacted and any("offload" in e.notice.reason for e in compacted)
    assert await store.get(result_digest(big)) == big


async def test_live_huge_tool_result_is_fixed_before_sending():
    """This turn's own tool returns several times the window. The view is
    compacted before it is sent — no call is refused or retried, every real
    prompt (the provider's own count) stays under the window, and the model
    still answers from the offload preview."""
    model = _live_model()

    @tool
    def read_log() -> str:
        """Read the service log."""
        noise = "noise line 0123456789 abcdefghij klmnopqrst uvwxyz\n" * 4_000
        return "FIRST LINE: incident code ZQ-5521.\n" + noise  # ~200K chars

    agent = _agent(
        model, "You are a concise assistant. Use tools when asked.", tools=[read_log]
    )
    window = 32_000
    result, seen = await _run_collect(
        agent,
        "Call read_log once, then reply with only the incident code from its "
        "first line.",
        context_policy=Compaction(context_window=window),
    )

    assert "ZQ-5521" in (result.output or "")
    prompts = [
        e.usage.input_tokens
        for e in seen
        if isinstance(e, events.MessageCompleted) and e.usage is not None
    ]
    assert len(prompts) >= 2  # the tool call, then the answer
    assert max(prompts) < window
    compacted = _compactions(seen)
    assert compacted and all(not e.notice.reactive for e in compacted)
    assert not any(isinstance(e, events.ModelRetrying) for e in seen)


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
    tokens_after = compacted[-1].notice.tokens_after
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
    with pytest.raises(ContextOverflowError) as exc_info:
        await Runner.run(
            agent,
            "OK?",
            context_policy=NoopContextPolicy(),
            session=sess,
            session_id="s1",
        )

    # The endpoint names its limit in the rejection; that number is what turns
    # an unknown model into a correctly-sized one for the rest of the session.
    window = exc_info.value.reported_window
    assert window is not None, (
        "provider stated no limit; window_from_error needs a new pattern"
    )
    assert 1024 <= window <= 20_000_000


# ---------------------------------------------------------------------------
# 8. Heuristic accuracy against the endpoint's real token accounting
# ---------------------------------------------------------------------------


async def test_live_estimator_accuracy_across_content_types():
    """The UTF-8-byte heuristic must land within the calibration clamp for
    every representative content type — English, code, Chinese, mixed — so
    the ratio's job stays residual error, not systematic mis-weighting."""
    model = _live_model()
    import httpx

    from lovia.context import TokenCounter

    samples = {
        "en": (
            "The compaction pipeline treats the transcript as immutable and "
            "renders a per-call view instead. Sticky decisions are monotonic: "
            "a cleared tool result never reverts, and summary coverage only "
            "grows, which keeps the rendered prefix byte-stable so provider "
            "prompt caches stay warm across turns. Between bursts nothing is "
            "touched, which is the hysteresis that prevents thrashing."
        ),
        "code": (
            "def pressure(self, tokens):\n    return tokens / self.usable\n\n"
            "candidates = [w for w in (learned, advertised) if w is not None]\n"
            "return min(candidates) if candidates else None\n"
            'logger.info("window: endpoint reported %d for %r", reported, model)'
        ),
        "zh": (
            "上下文压缩的核心矛盾在于：模型看到的提示必须装进有限的窗口，而对话"
            "本身却在无限增长。粘性决策一旦做出就不再回退，摘要的覆盖范围只会向前"
            "推进，渲染出的前缀因此在字节层面保持稳定，供应商的提示缓存才能持续"
            "命中。水位线之间的滞后带避免了策略每一轮都抖动。"
        ),
        "mixed": (
            "在 lovia 的运行时里，preflight 负责串行门控：cancel check、budget、"
            "tool lookup 和审批流。Every rejection appends exactly one "
            "ToolResultEntry so calls never dangle, 运行不会挂起。"
        ),
    }
    counter = TokenCounter()

    async with httpx.AsyncClient(timeout=120) as client:
        base = os.environ["OPENAI_BASE_URL"].rstrip("/")

        async def prompt_tokens(text: str) -> int:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": text}],
                    "max_tokens": 16,
                    "temperature": 0,
                },
            )
            r.raise_for_status()
            return r.json()["usage"]["prompt_tokens"]

        framing = await prompt_tokens("hi") - 1
        for name, text in samples.items():
            real = await prompt_tokens(text) - framing
            est = counter.count_entry(_user(text))
            assert 0.5 <= real / est <= 1.6, (
                f"{name}: real {real} / est {est} = {real / est:.2f} outside "
                "the calibration clamp's comfortable range"
            )


async def test_live_padded_tool_output_stays_within_the_pre_send_margin():
    """Compaction acts before sending only past 1.4x the window, on the raw
    estimate. Column-padded tool output (psql tables, ``ls -l``, ``ps``) is
    where bytes/4 over-counts most: it must stay under that margin, or a
    result that fits could be offloaded on the estimate alone."""
    model = _live_model()
    import httpx

    from lovia.context import TokenCounter
    from lovia.context.compaction import _CERTAINLY_OVER

    rows = range(400)
    samples = {
        "psql": "\n".join(
            [" id  |      name       |   amount   | status  ", "-" * 46]
            + [
                f" {i:<4}| {'customer_' + str(i):<16}| {i * 37.5:>10.2f} | "
                f"{'paid' if i % 3 else 'pending':<8}"
                for i in rows
            ]
        ),
        "box": "\n".join(
            ["┌──────┬──────────────────┬────────────┐"]
            + [f"│ {i:<4} │ {'item_' + str(i):<16} │ {i * 3:>10} │" for i in rows]
            + ["└──────┴──────────────────┴────────────┘"]
        ),
        "ls": "\n".join(
            f"-rw-r--r--   1 neo  staff   {i * 1013:>9} Nov 21 09:{i % 60:02d} "
            f"file_{i:04d}.log"
            for i in rows
        ),
        "ps": "\n".join(
            f"neo      {1000 + i:>6}   {i % 9}.{i % 7}  0.{i % 5}  "
            f"{4000000 + i:>8} {120000 + i:>7}   ??  S    9:{i % 60:02d}AM   "
            f"0:0{i % 9}.{i % 99:02d} /usr/bin/worker --id {i}"
            for i in rows
        ),
    }
    counter = TokenCounter()

    async with httpx.AsyncClient(timeout=120) as client:
        base = os.environ["OPENAI_BASE_URL"].rstrip("/")

        async def prompt_tokens(text: str) -> int:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": text}],
                    "max_tokens": 16,
                    "temperature": 0,
                },
            )
            r.raise_for_status()
            return r.json()["usage"]["prompt_tokens"]

        framing = await prompt_tokens("hi") - 1
        overcount = {}
        for name, text in samples.items():
            real = await prompt_tokens(text) - framing
            overcount[name] = counter.count_entry(_user(text)) / real
        print(f"\nbytes/4 over-count: {overcount}")
        for name, factor in overcount.items():
            assert factor < _CERTAINLY_OVER, (
                f"{name}: estimate is {factor:.2f}x the real count, past the "
                f"{_CERTAINLY_OVER}x pre-send margin"
            )
