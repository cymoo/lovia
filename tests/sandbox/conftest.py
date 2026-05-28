"""Shared fixtures for sandbox tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.run_context import RunContext


def make_ctx(session_id: str | None = None) -> RunContext:
    return RunContext(
        context=None,
        messages=[],
        agent=None,  # type: ignore[arg-type]
        session_id=session_id,
    )


@pytest.fixture
def ctx() -> RunContext:
    return make_ctx("s1")


@pytest.fixture
def seeded_root(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("print('b')\n", encoding="utf-8")
    return tmp_path
