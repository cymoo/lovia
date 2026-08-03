"""Shared isolation for the web test suite.

The providers fall back to the credential env vars, so a developer's real
configuration must never leak into (or be touched by) tests. Tests that
exercise the wizard's project-scope save isolate the filesystem with
``monkeypatch.chdir(tmp_path)`` themselves; ``~`` is redirected for every
test below, since the CLI reads and writes ``~/.lovia/config.json``.
"""

from __future__ import annotations

import json
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


@pytest.fixture
def write_config(fake_home: Path):
    """Write a ``config.json`` the CLI will pick up (user scope by default).

    The single-profile shortcut covers most tests; pass ``models=``/``roles=``
    for multi-profile shapes. ``scope="project"`` writes ``./.lovia`` relative
    to the current directory — call after ``monkeypatch.chdir(tmp_path)``.
    """

    def _write(
        model: str = "openai:gpt-x",
        *,
        api_key: str | None = "sk-abcdefghijkl9876",
        base_url: str | None = None,
        context_window: int | None = None,
        vision: str = "auto",
        scope: str = "user",
        models: list[dict] | None = None,
        roles: dict | None = None,
        search: dict | None = None,
    ) -> Path:
        if models is None:
            profile: dict = {"id": "default", "model": model, "vision": vision}
            if api_key:
                profile["api_key"] = api_key
            if base_url:
                profile["base_url"] = base_url
            if context_window:
                profile["context_window"] = context_window
            models = [profile]
        doc: dict = {"version": 1, "models": models}
        if roles is not None:
            doc["roles"] = roles
        if search is not None:
            doc["search"] = search
        root = fake_home if scope == "user" else Path.cwd()
        target = root / ".lovia" / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc), encoding="utf-8")
        return target

    return _write


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
