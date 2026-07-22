"""``lovia.model_from_env`` — the one blessed env lookup for a model id."""

from __future__ import annotations

import pytest

from lovia import UserError, model_from_env

_VARS = ("LOVIA_MODEL", "OPENAI_DEFAULT_MODEL", "ANTHROPIC_DEFAULT_MODEL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)


def test_reads_lovia_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-5.5")
    assert model_from_env() == "openai:gpt-5.5"


def test_bare_lovia_model_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bare id (no vendor prefix) is returned unchanged; Agent() routes it to
    # the OpenAI-compatible provider — the OPENAI_BASE_URL path (DeepSeek,
    # Ollama, vLLM).
    monkeypatch.setenv("LOVIA_MODEL", "deepseek-v4-pro")
    assert model_from_env() == "deepseek-v4-pro"


def test_legacy_default_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # LOVIA_MODEL is the single knob; the old OPENAI_/ANTHROPIC_DEFAULT_MODEL
    # fallbacks were removed.
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_MODEL", "claude-4-8-opus")
    assert model_from_env(required=False) is None


def test_missing_raises_with_hint() -> None:
    with pytest.raises(UserError, match="LOVIA_MODEL"):
        model_from_env()


def test_missing_optional_returns_none() -> None:
    assert model_from_env(required=False) is None
