"""``lovia.model_from_env`` — the one blessed env lookup for a model id."""

from __future__ import annotations

import pytest

from lovia import UserError, model_from_env

_VARS = ("LOVIA_MODEL", "OPENAI_DEFAULT_MODEL", "ANTHROPIC_DEFAULT_MODEL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)


def test_lovia_model_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_MODEL", "openai:gpt-5.5")
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "other")
    assert model_from_env() == "openai:gpt-5.5"


def test_openai_default_passes_through_bare(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bare ids route to the OpenAI-compatible provider — the OPENAI_BASE_URL
    # path (DeepSeek, Ollama, vLLM) — so no prefix is added.
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "deepseek-v4-pro")
    assert model_from_env() == "deepseek-v4-pro"


def test_anthropic_default_gets_prefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_DEFAULT_MODEL", "claude-4-8-opus")
    assert model_from_env() == "anthropic:claude-4-8-opus"


def test_anthropic_default_keeps_existing_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_DEFAULT_MODEL", "anthropic:claude-4-8-opus")
    assert model_from_env() == "anthropic:claude-4-8-opus"


def test_missing_raises_with_hint() -> None:
    with pytest.raises(UserError, match="LOVIA_MODEL"):
        model_from_env()


def test_missing_optional_returns_none() -> None:
    assert model_from_env(required=False) is None
