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
# pair, a session-scoped permission, an incidental health detail. Most lines
# are deliberately UNDATED — real pre-upgrade files are — so the eval covers
# stamping-on-keep and the leading-position format contract; only the
# superseded pair carries dates, since newer-wins needs them.
# ---------------------------------------------------------------------------

LEGACY_NOTES = [
    "Never delete files or directories without explicit user confirmation.",
    "Before deleting anything, list what would be removed and wait for the user to confirm.",
    "Prefer the gh CLI for GitHub operations instead of opening a browser.",
    "用户要求回答时效性问题前，先确认当前日期。",
    "The user runs macOS and manages packages with Homebrew.",
    "The user is learning the ukulele and has no prior music background.",
    "The user's ukulele goal piece is 'Over the Rainbow'.",
    "The user is a complete beginner at music theory.",
    "The corporate proxy blocks registry.npmjs.org; use the internal mirror at npm.corp.example.",
    "assets.example-cdn.net is reachable and can be used for demo images.",
    "Project petshop uses pnpm as its package manager.",
    "The petshop repo lives at ~/work/petshop.",
    "The pdf helper was installed to ~/.tools/pdf/ with SKILL.md and 8 Python scripts.",
    "The docx helper was installed to ~/.tools/docx/ (61 files, 1.2M).",
    "The pptx helper was installed to ~/.tools/pptx/ (56 files, 1.2M).",
    "The xlsx helper was installed to ~/.tools/xlsx/ (53 files, 1.2M).",
    # A pre-merged bundle from an earlier, laxer pass: a droppable directory
    # listing laundered together with a keepable environment gotcha.
    "~/.tools contains pdf, docx, pptx and xlsx helpers plus an unidentified folder gen-1.0.2 (origin unconfirmed); because npx times out on this network, helpers are installed by cloning their GitHub repo instead.",
    # A deliverable with its provenance visible. (A cue-less "weekly workout
    # plan: …" is indistinguishable from the user's own regimen by text alone
    # — a maintenance pass rightly keeps it, so the eval doesn't demand
    # otherwise; only fresh admission can prevent cue-less deliverables.)
    "Generated a 7-day workout plan: Monday legs, Tuesday push, Wednesday cardio, Thursday pull, Friday full-body, Saturday recovery, Sunday rest.",
    "The workout plan includes hydration and sleep advice and progresses every 4 weeks.",
    "Downloading long videos: start right after obtaining the link because auth tokens on cdn.example-media.com expire mid-download.",
    "For videos with more than 256 segments on macOS, pass --use-ffmpeg-concat-demuxer to avoid file-handle limits.",
    "[2025-12] The staging database lives at ./staging.db in the repo root.",
    "[2026-06] The staging database actually lives at .data/staging.db, not the repo root.",
    "The user allowed direct edits to the staging database during today's debugging session.",
    "The user mentioned trouble sleeping lately and is looking into causes.",
    "用户偏好简体中文回复，语气自然不要过度客套。",
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
        if not ok:
            # Temperature 0 is not determinism (MoE providers flake roughly
            # one run in three on a marginal case); a single retry keeps the
            # signal and the "(retry)" label keeps the flake visible.
            digest = await _digest(_msgs(*convo), current, model)
            ok = verdict(digest)
            name = f"{name} (retry)"
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

    # Substantially smaller, and every line carries a LEADING stamp — the
    # format contract that broke on real all-undated files (models appended
    # the date instead; _relocate_stamp plus the prompt now pin it down).
    assert 0 < len(result) <= 18
    assert all(re.match(r"^\[\d{4}-\d{2}\]", f) for f in result)

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
        r"gen-1\.0\.2",
    ):
        assert not re.search(pattern, joined, re.IGNORECASE), pattern

    # The keepable half of the laundered bundle survives the split-then-judge.
    assert re.search(r"clon", joined, re.IGNORECASE)

    # The near-duplicate deletion rules merged: at most one line says it.
    deletion_rules = [
        f
        for f in result
        if re.search(r"delet", f, re.IGNORECASE) and "confirm" in f.lower()
    ]
    assert len(deletion_rules) <= 1

    # The three ukulele lines collapsed to at most two.
    assert sum(1 for f in result if "ukulele" in f.lower()) <= 2

    # …but merging must stop at subject boundaries: no mega-note bundling
    # distinct rules as an enumerated list (observed on real data as
    # "core rules: (1)…(6)"), and the deletion rule must not swallow the
    # unrelated gh-CLI or date-check rules.
    assert not re.search(r"\([1-9]\)|（[1-9]）", joined)
    assert not any(re.search(r"\bgh\b|日期", f) for f in deletion_rules)
