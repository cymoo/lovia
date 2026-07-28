"""Shared isolation for the web test suite.

The CLI reads the provider credential env vars, so a developer's real
configuration must never leak into (or be touched by) tests. Tests that
exercise the wizard's project-scope save isolate the filesystem with
``monkeypatch.chdir(tmp_path)`` themselves; ``~`` is redirected for every
test below, since the CLI reads and writes ``~/.lovia/config.env``.
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
    "LOVIA_WEB_TOKEN",
    "LOVIA_MAX_UPLOAD_MB",
    "LOVIA_UPLOAD_ALLOWED_EXT",
)


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _LEAKY_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def fake_home(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> Path:
    """Point ``~`` at a fresh per-test dir — never the developer's real one.

    Deliberately *outside* ``tmp_path`` (many tests chdir there and use it as
    a workspace root), so the project scope (``./.lovia``), the user scope
    (``~/.lovia``) and workspace listings never bleed into each other.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows expanduser
    return home


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
