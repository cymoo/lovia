"""Opt-in curation-quality eval for the memory digest and dream prompts.

Prompt changes are empirical: this eval feeds the *live* model synthetic
conversations that mirror the failure modes observed in real MEMORY.md files
(install logs promoted to facts, paraphrase duplicates, one-off task detail,
session-scoped permissions fossilized) and checks what the digest admits,
retires, and what a dream keeps.

The fixture notes are entirely fictional — they reproduce the pathologies,
not any real user's data.

Run with the chat endpoint from ``.env``::

    LOVIA_LIVE_TESTS=1 uv run pytest tests/plugins/test_memory_curation_eval.py -m live_provider -s
"""

from __future__ import annotations

import re

import pytest

from lovia.plugins.memory.plugin import _digest, _dream, _RunDigest

from .test_memory import _live_model, _msgs

# ---------------------------------------------------------------------------
# Digest admission: (name, current notes, conversation, verdict)
# ---------------------------------------------------------------------------


def _no_facts_matching(digest: _RunDigest, pattern: str) -> bool:
    return not any(re.search(pattern, f, re.IGNORECASE) for f in digest.facts)


DIGEST_CASES = [
    (
        "explicit-preference",
        "",
        [
            (
                "user",
                "From now on, always write commit messages in English, even "
                "when we chat in Chinese.",
            ),
            ("assistant", "Got it — English commit messages from here on."),
        ],
        lambda d: (
            1 <= len(d.facts) <= 3
            and any(re.search(r"commit|english", f, re.IGNORECASE) for f in d.facts)
        ),
    ),
    (
        "explicit-preference-zh",
        "",
        [
            ("user", "请记住：以后所有代码注释都用英文写，聊天继续用中文。"),
            ("assistant", "好的，已记住：代码注释用英文，对话用中文。"),
        ],
        lambda d: len(d.facts) >= 1 and any("英文" in f for f in d.facts),
    ),
    (
        "correction-supersedes",
        "- [2026-01] The staging database lives at ./staging.db in the repo root.",
        [
            (
                "user",
                "Heads up — we moved the staging database to .data/staging.db "
                "last week. Please update your notes.",
            ),
            ("assistant", "Noted: the staging database is at .data/staging.db now."),
        ],
        lambda d: (
            any(".data/staging.db" in f for f in d.facts)
            and any("staging" in s.lower() for s in d.stale)
        ),
    ),
    (
        "install-log",
        "",
        [
            ("user", "Install the report helper for me."),
            (
                "assistant",
                "Installed report-helper 2.1 into ~/.tools/report (48 files, "
                "900K). It provides gen_report.py and a template pack.",
            ),
        ],
        lambda d: _no_facts_matching(d, r"48 files|900K|2\.1"),
    ),
    (
        "transient-task",
        "",
        [
            ("user", "The tests fail with an ImportError in test_foo — fix it."),
            (
                "assistant",
                "Fixed: test_foo was missing an import of helpers.load; "
                "the suite passes now.",
            ),
        ],
        lambda d: len(d.facts) == 0,
    ),
    (
        "session-permission",
        "",
        [
            (
                "user",
                "For this task only, you may rewrite rows in the test database "
                "directly.",
            ),
            ("assistant", "Understood — I'll edit the rows for this task."),
        ],
        lambda d: _no_facts_matching(d, r"allow|permission|may (edit|rewrite)"),
    ),
    (
        "sensitive-incidental",
        "",
        [
            (
                "user",
                "I've been sleeping badly lately, no idea why… anyway, help me "
                "rename these three files to kebab-case.",
            ),
            ("assistant", "Renamed all three to kebab-case."),
        ],
        lambda d: _no_facts_matching(d, r"sleep|insomnia"),
    ),
    (
        "derivable-state",
        "",
        [
            ("user", "Which Python does this project need?"),
            (
                "assistant",
                'pyproject.toml says requires-python = ">=3.13", so 3.13 or newer.',
            ),
        ],
        lambda d: _no_facts_matching(d, r"3\.13|requires-python"),
    ),
]


# ---------------------------------------------------------------------------
# Dream regression fixture — a fictional bloated notes file reproducing the
# observed pathologies: duplicated rules, an interest split across three
# lines, install logs with counts and sizes, task deliverables, a superseded
# pair, a session-scoped permission, an incidental health detail.
# ---------------------------------------------------------------------------

LEGACY_NOTES = [
    "[2025-11] Never delete files or directories without explicit user confirmation.",
    "[2026-03] Before deleting anything, list what would be removed and wait for the user to confirm.",
    "[2026-01] Prefer the gh CLI for GitHub operations instead of opening a browser.",
    "[2025-12] 用户要求回答时效性问题前，先确认当前日期。",
    "[2025-11] The user runs macOS and manages packages with Homebrew.",
    "[2026-02] The user is learning the ukulele and has no prior music background.",
    "[2026-02] The user's ukulele goal piece is 'Over the Rainbow'.",
    "[2026-03] The user is a complete beginner at music theory.",
    "[2026-04] The corporate proxy blocks registry.npmjs.org; use the internal mirror at npm.corp.example.",
    "[2026-04] assets.example-cdn.net is reachable and can be used for demo images.",
    "[2026-01] Project petshop uses pnpm as its package manager.",
    "[2026-01] The petshop repo lives at ~/work/petshop.",
    "[2026-05] The pdf helper was installed to ~/.tools/pdf/ with SKILL.md and 8 Python scripts.",
    "[2026-05] The docx helper was installed to ~/.tools/docx/ (61 files, 1.2M).",
    "[2026-05] The pptx helper was installed to ~/.tools/pptx/ (56 files, 1.2M).",
    "[2026-05] The xlsx helper was installed to ~/.tools/xlsx/ (53 files, 1.2M).",
    "[2026-05] ~/.tools currently contains pdf, docx, pptx, xlsx and an unidentified folder gen-1.0.2 (origin unconfirmed).",
    "[2026-02] Generated a 7-day workout plan: Monday legs, Tuesday push, Wednesday cardio, Thursday pull, Friday full-body, Saturday recovery, Sunday rest.",
    "[2026-02] The workout plan includes hydration and sleep advice and progresses every 4 weeks.",
    "[2026-06] Downloading long videos: start right after obtaining the link because auth tokens on cdn.example-media.com expire mid-download.",
    "[2026-06] For videos with more than 256 segments on macOS, pass --use-ffmpeg-concat-demuxer to avoid file-handle limits.",
    "[2025-12] The staging database lives at ./staging.db in the repo root.",
    "[2026-06] The staging database actually lives at .data/staging.db, not the repo root.",
    "[2026-03] The user allowed direct edits to the staging database during today's debugging session.",
    "[2026-04] The user mentioned trouble sleeping lately and is looking into causes.",
    "[2026-05] 用户偏好简体中文回复，语气自然不要过度客套。",
]


def _print_rows(title: str, rows: list[tuple[str, bool, str]]) -> None:
    width = max(len(name) for name, _, _ in rows)
    print(f"\n{title}")
    for name, ok, note in rows:
        print(f"  {name.ljust(width)}  {'PASS' if ok else 'FAIL'}  {note}")


@pytest.mark.live_provider
async def test_digest_admission_eval() -> None:
    model = f"openai:{_live_model()}"
    rows: list[tuple[str, bool, str]] = []
    failures: list[str] = []
    for name, current, convo, verdict in DIGEST_CASES:
        digest = await _digest(_msgs(*convo), current, model)
        ok = verdict(digest)
        rows.append((name, ok, f"facts={digest.facts!r} stale={digest.stale!r}"))
        if not ok:
            failures.append(name)
    _print_rows("digest admission", rows)
    assert not failures, f"admission cases failed: {failures}"


@pytest.mark.live_provider
async def test_dream_eval() -> None:
    model = f"openai:{_live_model()}"
    body = "\n".join(f"- {f}" for f in LEGACY_NOTES)
    result = await _dream(body, 5000, model)
    joined = "\n".join(result)
    print(f"\ndream: {len(LEGACY_NOTES)} → {len(result)} notes\n{joined}")

    # Substantially smaller, and still dated lines.
    assert 0 < len(result) <= 18
    dated = sum(1 for f in result if re.match(r"^\[\d{4}-\d{2}\]", f))
    assert dated >= 0.8 * len(result)

    # Explicit rules and stable profile survive (possibly merged/rephrased).
    assert re.search(
        r"delet.*confirm|confirm.*delet", joined, re.IGNORECASE | re.DOTALL
    )
    assert "gh" in joined
    assert "Homebrew" in joined
    assert re.search(r"ukulele", joined, re.IGNORECASE)
    assert re.search(r"proxy|mirror", joined, re.IGNORECASE)
    # The Chinese preference stays in Chinese.
    assert "中文" in joined

    # Newer-wins on the superseded pair.
    assert ".data/staging.db" in joined
    assert "./staging.db in the repo root" not in joined

    # Install logs, deliverables, session permissions, incidental health
    # details, and dangling TODOs are gone.
    for pattern in (
        r"61 files|1\.2M|53 files|56 files",
        r"workout|Monday legs",
        r"debugging session",
        r"trouble sleeping",
        r"origin unconfirmed",
    ):
        assert not re.search(pattern, joined, re.IGNORECASE), pattern

    # The near-duplicate deletion rules merged: at most one line says it.
    deletion_rules = [
        f
        for f in result
        if re.search(r"delet", f, re.IGNORECASE) and "confirm" in f.lower()
    ]
    assert len(deletion_rules) <= 1

    # The three ukulele lines collapsed to at most two.
    assert sum(1 for f in result if "ukulele" in f.lower()) <= 2
