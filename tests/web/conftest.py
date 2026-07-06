"""Shared isolation for the web test suite.

The CLI now auto-loads ``~/.config/lovia/config.env`` and reads the
provider credential env vars, so a developer's real configuration must
never leak into (or be touched by) tests.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

_LEAKY_VARS = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "LOVIA_MODEL",
    "OPENAI_DEFAULT_MODEL",
    "ANTHROPIC_DEFAULT_MODEL",
    "LOVIA_CONTEXT_WINDOW",
)


@pytest.fixture(autouse=True)
def _isolate_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in _LEAKY_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _restore_lovia_logger() -> Iterator[None]:
    """Undo enable_logging() side effects from main() tests.

    It sets propagate=False on the "lovia" logger, which would silently
    break caplog for every later test in the session.
    """
    logger = logging.getLogger("lovia")
    level, propagate, handlers = logger.level, logger.propagate, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.propagate = propagate
    logger.handlers[:] = handlers
