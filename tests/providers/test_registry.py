"""Tests for the provider registry / entry-point plugin path."""

from __future__ import annotations

import pytest

from lovia.exceptions import UserError
from lovia.providers import model_from_env, provider_from_string, register_provider


class _FakeProvider:
    """Bare minimum that ``provider_from_string`` should hand back unchanged."""

    def __init__(self, model: str) -> None:
        self.model = model


def test_unknown_prefix_raises_with_actionable_message() -> None:
    with pytest.raises(UserError) as excinfo:
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


def test_register_provider_overrides_builtin_prefix() -> None:
    from lovia.providers import _REGISTRY

    register_provider("openai", lambda model: _FakeProvider(f"override:{model}"))
    try:
        p = provider_from_string("openai:gpt")
        assert isinstance(p, _FakeProvider)
        assert p.model == "override:gpt"
    finally:
        _REGISTRY.pop("openai", None)
    # With the registration removed, the built-in factory applies again.
    assert provider_from_string("openai:gpt").name == "openai-chat"


def test_register_provider_later_registration_wins() -> None:
    from lovia.providers import _REGISTRY

    try:
        register_provider("openaix", lambda model: _FakeProvider(f"v1:{model}"))
        register_provider("openaix", lambda model: _FakeProvider(f"v2:{model}"))
        p = provider_from_string("openaix:gpt")
        assert isinstance(p, _FakeProvider)
        assert p.model == "v2:gpt"
    finally:
        _REGISTRY.pop("openaix", None)


def test_register_provider_prefix_is_case_insensitive() -> None:
    from lovia.providers import _REGISTRY

    try:
        register_provider("FakeCo", lambda model: _FakeProvider(model))
        p = provider_from_string("FAKECO:fake-1")
        assert isinstance(p, _FakeProvider)
        assert p.model == "fake-1"
    finally:
        _REGISTRY.pop("fakeco", None)


@pytest.mark.parametrize(
    ("spec", "provider_name"),
    [
        ("openai:m1", "openai-chat"),
        ("oai:m1", "openai-chat"),
        ("openai-chat:m1", "openai-chat"),
        ("anthropic:m1", "anthropic"),
        ("claude:m1", "anthropic"),
        ("bare-model", "openai-chat"),
    ],
)
def test_api_key_and_base_url_overrides_reach_the_provider(
    spec: str, provider_name: str
) -> None:
    p = provider_from_string(spec, api_key="sk-test", base_url="http://gw:9/v1")

    assert p.name == provider_name
    assert p.base_url == "http://gw:9/v1"
    assert p._api_key == "sk-test"


def test_overrides_default_to_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitted overrides keep today's env-derived behavior."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://from-env/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    p = provider_from_string("openai:m1")

    assert p.base_url == "http://from-env/v1"
    assert p._api_key == "sk-env"


def test_partial_override_keeps_env_for_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    p = provider_from_string("openai:m1", base_url="http://gw/v1")

    assert p.base_url == "http://gw/v1"
    assert p._api_key == "sk-env"


def test_registered_factory_without_kwargs_still_works_without_overrides() -> None:
    from lovia.providers import _REGISTRY

    try:
        register_provider("plainco", lambda model: _FakeProvider(model))
        p = provider_from_string("plainco:m1")
        assert isinstance(p, _FakeProvider)
    finally:
        _REGISTRY.pop("plainco", None)


def test_registered_factory_without_kwargs_rejects_overrides() -> None:
    from lovia.providers import _REGISTRY

    try:
        register_provider("plainco", lambda model: _FakeProvider(model))
        with pytest.raises(UserError, match="does not accept api_key/base_url"):
            provider_from_string("plainco:m1", api_key="sk-x")
    finally:
        _REGISTRY.pop("plainco", None)


def test_entry_point_provider_class_receives_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata
    import lovia.providers as providers

    class _KwProvider:
        def __init__(
            self,
            model: str,
            *,
            api_key: str | None = None,
            base_url: str | None = None,
        ) -> None:
            self.model = model
            self.api_key = api_key
            self.base_url = base_url

    class _ClassEntryPoint:
        name = "kwclassy"
        value = "pkg:cls"

        def load(self) -> object:
            return _KwProvider

    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda group: [_ClassEntryPoint()]
    )
    monkeypatch.setattr(providers, "_ENTRY_POINTS", None)

    try:
        p = provider_from_string("kwclassy:m1", api_key="k", base_url="http://x")
    finally:
        providers._REGISTRY.pop("kwclassy", None)

    assert isinstance(p, _KwProvider)
    assert (p.api_key, p.base_url) == ("k", "http://x")


def test_bare_claude_model_warns_about_missing_prefix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="lovia.providers"):
        provider = provider_from_string("claude-sonnet-4-5")

    assert provider.name == "openai-chat"
    assert any("anthropic:claude-sonnet-4-5" in r.message for r in caplog.records)


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

    with pytest.raises(UserError, match="failed to load"):
        provider_from_string("broken:model")


def test_entry_point_may_export_a_provider_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata
    import lovia.providers as providers

    class _ClassEntryPoint:
        name = "classy"
        value = "pkg:cls"

        def load(self) -> object:
            return _FakeProvider

    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda group: [_ClassEntryPoint()]
    )
    monkeypatch.setattr(providers, "_ENTRY_POINTS", None)

    try:
        p = provider_from_string("classy:model-1")
    finally:
        # Successful entry-point loads are cached into the runtime registry.
        providers._REGISTRY.pop("classy", None)

    assert isinstance(p, _FakeProvider)
    assert p.model == "model-1"


def test_entry_point_rejects_non_callable_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata
    import lovia.providers as providers

    class _BadEntryPoint:
        name = "bad"
        value = "pkg:obj"

        def load(self) -> object:
            return object()

    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda group: [_BadEntryPoint()]
    )
    monkeypatch.setattr(providers, "_ENTRY_POINTS", None)

    with pytest.raises(UserError, match="provider class or callable factory"):
        provider_from_string("bad:model")


# ---------------------------------------------------------------------------
# model_from_env
# ---------------------------------------------------------------------------


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LOVIA_MODEL", "OPENAI_DEFAULT_MODEL", "ANTHROPIC_DEFAULT_MODEL"):
        monkeypatch.delenv(var, raising=False)


def test_model_from_env_reads_lovia_model(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("LOVIA_MODEL", "openai:a")
    assert model_from_env() == "openai:a"


def test_model_from_env_ignores_legacy_default_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_model_env(monkeypatch)
    # LOVIA_MODEL is the single knob; OPENAI_/ANTHROPIC_DEFAULT_MODEL are no
    # longer consulted.
    monkeypatch.setenv("OPENAI_DEFAULT_MODEL", "b")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_MODEL", "c")
    assert model_from_env(required=False) is None


def test_model_from_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_model_env(monkeypatch)
    assert model_from_env(required=False) is None
    with pytest.raises(UserError, match="no model configured"):
        model_from_env()
