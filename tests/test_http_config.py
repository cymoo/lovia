from __future__ import annotations

import ssl
import sys
import types

import certifi
import pytest

from lovia.http_config import resolve_timeout, resolve_trust_env, resolve_verify

# --------------------------------------------------------------- verify -


def test_resolve_verify_defaults_to_certifi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_HTTP_INSECURE", raising=False)
    monkeypatch.delenv("LOVIA_HTTP_CA_BUNDLE", raising=False)
    # Setting the module to None makes ``import truststore`` raise ImportError,
    # forcing the certifi fallback regardless of what is installed.
    monkeypatch.setitem(sys.modules, "truststore", None)
    assert isinstance(resolve_verify(), ssl.SSLContext)


def test_resolve_verify_uses_truststore_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOVIA_HTTP_INSECURE", raising=False)
    monkeypatch.delenv("LOVIA_HTTP_CA_BUNDLE", raising=False)
    sentinel = ssl.create_default_context()
    fake = types.SimpleNamespace(SSLContext=lambda proto: sentinel)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    assert resolve_verify() is sentinel


def test_resolve_verify_ca_bundle_beats_truststore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOVIA_HTTP_INSECURE", raising=False)
    sentinel = ssl.create_default_context()
    fake = types.SimpleNamespace(SSLContext=lambda proto: sentinel)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    monkeypatch.setenv("LOVIA_HTTP_CA_BUNDLE", certifi.where())
    assert resolve_verify() is not sentinel  # explicit CA bundle wins


def test_resolve_verify_uses_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_HTTP_INSECURE", raising=False)
    # A real bundle builds a context; a missing one fails fast (proving the
    # configured path is actually consulted).
    monkeypatch.setenv("LOVIA_HTTP_CA_BUNDLE", certifi.where())
    assert isinstance(resolve_verify(), ssl.SSLContext)
    monkeypatch.setenv("LOVIA_HTTP_CA_BUNDLE", "/no/such/ca.pem")
    with pytest.raises(FileNotFoundError):
        resolve_verify()


def test_resolve_verify_insecure_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_HTTP_CA_BUNDLE", certifi.where())
    monkeypatch.setenv("LOVIA_HTTP_INSECURE", "1")
    assert resolve_verify() is False


def test_resolve_verify_insecure_accepts_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOVIA_HTTP_CA_BUNDLE", raising=False)
    for val in ("true", "yes", "on", "ON", " True "):
        monkeypatch.setenv("LOVIA_HTTP_INSECURE", val)
        assert resolve_verify() is False
    for val in ("0", "false", "no", ""):
        monkeypatch.setenv("LOVIA_HTTP_INSECURE", val)
        assert resolve_verify() is not False  # verification stays on


# -------------------------------------------------------------- timeout -


def test_resolve_timeout_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_PROVIDER_TIMEOUT", raising=False)
    assert resolve_timeout(None) == 300.0


def test_resolve_timeout_explicit_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_PROVIDER_TIMEOUT", "120")
    assert resolve_timeout(30.0) == 30.0


def test_resolve_timeout_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_PROVIDER_TIMEOUT", "120")
    assert resolve_timeout(None) == 120.0


def test_resolve_timeout_ignores_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_PROVIDER_TIMEOUT", "not-a-number")
    assert resolve_timeout(None) == 300.0


def test_resolve_timeout_ignores_non_positive_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOVIA_PROVIDER_TIMEOUT", "0")
    assert resolve_timeout(None) == 300.0


# ------------------------------------------------------------ trust_env -


def test_resolve_trust_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOVIA_PROVIDER_TRUST_ENV", raising=False)
    assert resolve_trust_env(None) is False


def test_resolve_trust_env_explicit_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_PROVIDER_TRUST_ENV", "1")
    assert resolve_trust_env(False) is False


def test_resolve_trust_env_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "true", "yes", "on", "ON", "True"):
        monkeypatch.setenv("LOVIA_PROVIDER_TRUST_ENV", val)
        assert resolve_trust_env(None) is True


def test_resolve_trust_env_falsey_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("0", "false", "no", ""):
        monkeypatch.setenv("LOVIA_PROVIDER_TRUST_ENV", val)
        assert resolve_trust_env(None) is False
