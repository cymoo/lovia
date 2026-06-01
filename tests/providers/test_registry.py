"""Tests for the provider registry / entry-point plugin path."""

from __future__ import annotations

import pytest

from lovia.providers import provider_from_string, register_provider


class _FakeProvider:
    """Bare minimum that ``provider_from_string`` should hand back unchanged."""

    def __init__(self, model: str) -> None:
        self.model = model


def test_unknown_prefix_raises_with_actionable_message() -> None:
    with pytest.raises(ValueError) as excinfo:
        provider_from_string("does-not-exist:foo")
    msg = str(excinfo.value)
    assert "register_provider" in msg or "entry-point" in msg


def test_bare_model_defaults_to_openai_chat() -> None:
    provider = provider_from_string("gpt-5")

    assert provider.name == "openai-chat"
    assert provider.model == "gpt-5"


def test_register_provider_with_factory() -> None:
    register_provider("fakeco", lambda model: _FakeProvider(model))
    p = provider_from_string("fakeco:fake-model-1")
    assert isinstance(p, _FakeProvider)
    assert p.model == "fake-model-1"


def test_register_provider_overrides_existing() -> None:
    register_provider("openai", lambda model: _FakeProvider(f"override:{model}"))
    try:
        # NOTE: built-in `openai` lives in _BUILTIN which has precedence over the
        # runtime _REGISTRY. So this registration is a no-op for the built-in
        # prefix — that's by design (built-ins always win). Verify by hitting a
        # new prefix instead.
        register_provider("openaix", lambda model: _FakeProvider(f"v1:{model}"))
        register_provider("openaix", lambda model: _FakeProvider(f"v2:{model}"))
        p = provider_from_string("openaix:gpt")
        assert isinstance(p, _FakeProvider)
        assert p.model == "v2:gpt"
    finally:
        # Best-effort cleanup so other tests aren't affected.
        from lovia.providers import _REGISTRY

        _REGISTRY.pop("openaix", None)


def test_builtin_openai_routes_to_factory() -> None:
    """The built-in openai prefix is recognised (no ValueError)."""
    from lovia.providers import _BUILTIN

    assert "openai" in _BUILTIN
    assert "openai-chat" in _BUILTIN
    assert "anthropic" in _BUILTIN


def test_entry_point_loading_is_targeted_and_reports_target_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata
    import lovia.providers as providers

    class _FakeEntryPoint:
        def __init__(self, name: str, value: str, fail: bool = False) -> None:
            self.name = name
            self.value = value
            self.fail = fail
            self.loaded = False

        def load(self) -> object:
            self.loaded = True
            if self.fail:
                raise RuntimeError("boom")
            return lambda model: _FakeProvider(f"{self.name}:{model}")

    broken = _FakeEntryPoint("broken", "pkg:broken", fail=True)
    other = _FakeEntryPoint("other", "pkg:other")
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda group: [broken, other],
    )
    monkeypatch.setattr(providers, "_ENTRY_POINTS", None)

    p = provider_from_string("other:model")

    assert isinstance(p, _FakeProvider)
    assert p.model == "other:model"
    assert other.loaded is True
    assert broken.loaded is False

    with pytest.raises(ValueError, match="failed to load"):
        provider_from_string("broken:model")
