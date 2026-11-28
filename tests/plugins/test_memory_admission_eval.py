"""Opt-in admission eval: what the agent itself chooses to remember.

The agent is the only writer of Notes, so the whole admission policy is two
pieces of prompt — ``remember``'s description and the Memory section. Prompt
changes are empirical: this eval puts the *live* model in front of
conversations that mirror what real MEMORY.md files filled up with (task
detail, repo facts, guesses about the user, one-off requests, the assistant's
own claims) next to the few things worth keeping, and checks both directions:
nothing saved that shouldn't be, nothing missed that should.

The cases are fictional — they reproduce the pathologies, not any real
user's data.

Run with the chat endpoint from ``.env``::

    LOVIA_LIVE_TESTS=1 uv run pytest tests/plugins/test_memory_admission_eval.py -m live_provider -s
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from lovia import Agent, ModelSettings, Runner
from lovia.messages import Message
from lovia.plugins.memory.plugin import Memory, _strip_date

from .test_memory import _live_model

Turns = list[tuple[str, str]]


def _any(facts: list[str], pattern: str) -> bool:
    return any(re.search(pattern, f, re.IGNORECASE) for f in facts)


def _none(pattern: str) -> Callable[[list[str]], bool]:
    return lambda notes: not _any(notes, pattern)


def _nothing(notes: list[str]) -> bool:
    return notes == []


# (name, notes before, conversation ending on a user turn, verdict on notes after)
CASES: list[tuple[str, str, Turns, Callable[[list[str]], bool]]] = [
    # -- worth keeping -------------------------------------------------------
    (
        "standing-preference",
        "",
        [
            (
                "user",
                "From now on, always write commit messages in English, even "
                "when we chat in Chinese.",
            )
        ],
        lambda n: 1 <= len(n) <= 2 and _any(n, r"commit"),
    ),
    (
        "explicit-ask-zh",
        "",
        [("user", "请记住：以后所有代码注释都用英文写，聊天继续用中文。")],
        lambda n: 1 <= len(n) <= 2 and _any(n, "英文"),
    ),
    (
        "correction-of-how-to-work",
        "",
        [
            ("user", "帮我装一下 requests"),
            ("assistant", "好的，运行 `pip install requests` 即可。"),
            ("user", "别再让我用 pip 了，这台机器上我一律用 uv，说过好几次了。"),
        ],
        lambda n: len(n) == 1 and _any(n, "uv"),
    ),
    (
        "note-superseded",
        "- [2026-01] The staging database lives at ./staging.db in the repo root.",
        [
            (
                "user",
                "Heads up — we moved the staging database to .data/staging.db "
                "last week. Please update your notes.",
            )
        ],
        # Replaced, not added beside the old one.
        lambda n: len(n) == 1 and _any(n, r"\.data/staging\.db"),
    ),
    (
        "where-things-are",
        "",
        [
            (
                "user",
                "My notes live in a tool called mote on my own server. The CLI "
                "is already set up here — just call mote.py, and don't ask me "
                "for credentials again.",
            )
        ],
        lambda n: 1 <= len(n) <= 2 and _any(n, "mote"),
    ),
    # -- not worth keeping ---------------------------------------------------
    (
        "task-at-hand",
        "",
        [("user", "The tests fail with an ImportError in test_foo — what's wrong?")],
        _nothing,
    ),
    (
        "permission-for-this-task",
        "",
        [
            (
                "user",
                "For this task only, you may rewrite rows in the test database "
                "directly. Start by telling me which rows you'd change.",
            )
        ],
        _none(r"allow|permission|may (edit|rewrite)|rewrite rows"),
    ),
    (
        "one-off-request",
        "",
        [("user", "这次用英文回答我：什么是幂等？")],
        _nothing,
    ),
    (
        "incidental-personal-detail",
        "",
        [
            (
                "user",
                "I've been sleeping badly lately, no idea why… anyway, what's "
                "the kebab-case form of `MyReportFile`?",
            )
        ],
        _none(r"sleep|insomnia"),
    ),
    (
        "guess-at-interests",
        "",
        [
            ("user", "网上说某家公司下周要发新模型，是真的吗？"),
            ("assistant", "目前没有官方消息，流传的截图来源不明，建议等正式公告。"),
            ("user", "那另一条说他们要开源旧模型的传闻呢？"),
        ],
        _nothing,
    ),
    (
        "general-knowledge",
        "",
        [
            (
                "user",
                "我在评测一个只报阳性结果的缺陷检测工具，指标应该怎么表述？",
            )
        ],
        _nothing,
    ),
    (
        "what-the-repo-records",
        "",
        [
            ("user", "这个项目要求哪个 Python 版本？"),
            (
                "assistant",
                'pyproject.toml 里写的是 requires-python = ">=3.13"，所以需要 3.13 及以上。',
            ),
            ("user", "好的，那依赖是用什么管理的？"),
        ],
        _none(r"3\.13|requires-python|pyproject"),
    ),
    (
        "deliverable",
        "",
        [("user", "帮我排一个四周的尤克里里入门练习计划，每天二十分钟。")],
        _nothing,
    ),
    (
        # The assistant's own claims are not facts — here a wrong one, which
        # on real data resurfaced in later sessions as a "fact".
        "assistants-own-claim",
        "",
        [
            ("user", "你能显示表格吗"),
            ("assistant", "可以，我有一个 data_grid 工具，能生成可排序的表格。"),
            ("user", "支持分页吗"),
            (
                "assistant",
                "不支持分页：data_grid 只有 title / columns / rows 三个参数。",
            ),
            ("user", "好的，知道了"),
        ],
        _nothing,
    ),
]


def _agent(mem: Memory) -> Agent:
    return Agent(
        name="assistant",
        model=f"openai:{_live_model()}",
        instructions="You are a concise, helpful assistant.",
        settings=ModelSettings(temperature=0),
        plugins=[mem],
    )


async def _notes_after(root: Path, before: str, turns: Turns) -> list[str]:
    mem = Memory(root, index=None)
    if before:
        await mem.replace_notes(before)
    messages = [Message(role=role, content=content) for role, content in turns]  # type: ignore[arg-type]
    await Runner.run(_agent(mem), messages)
    return [_strip_date(f) for f in await mem._user.notes.load()]


@pytest.mark.live_provider
async def test_admission_eval(tmp_path) -> None:
    rows: list[tuple[str, bool, str]] = []
    for name, before, turns, verdict in CASES:
        notes = await _notes_after(tmp_path / name, before, turns)
        ok = verdict(notes)
        if not ok:
            # Temperature 0 is not determinism; a single retry keeps the
            # signal and the "(retry)" label keeps the flake visible.
            notes = await _notes_after(tmp_path / f"{name}-retry", before, turns)
            ok = verdict(notes)
            name = f"{name} (retry)"
        rows.append((name, ok, repr(notes)))
    width = max(len(name) for name, _, _ in rows)
    print("\nadmission")
    for name, ok, note in rows:
        print(f"  {name.ljust(width)}  {'PASS' if ok else 'FAIL'}  {note}")
    failures = [name for name, ok, _ in rows if not ok]
    assert not failures, f"admission cases failed: {failures}"


@pytest.mark.live_provider
async def test_layer_choice_eval(tmp_path) -> None:
    # With two layers, does the model file each fact under the right one?
    mem = Memory(tmp_path, index=None, scope=lambda ctx: "/work/petshop")
    await Runner.run(
        _agent(mem),
        "Please remember two things: I always want answers short and to the "
        "point, and in this repo releases are cut only from the `release` "
        "branch — that rule isn't written down anywhere.",
    )
    user = [_strip_date(f) for f in await mem._tier(None).notes.load()]
    project = [_strip_date(f) for f in await mem._tier("/work/petshop").notes.load()]
    print(f"\nlayer choice: user={user!r} project={project!r}")
    assert _any(user, r"short|concise|point") and not _any(user, r"release")
    assert _any(project, r"release") and not _any(project, r"short|concise")
