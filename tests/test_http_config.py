from __future__ import annotations

import ssl
import sys
import types

import certifi
import pytest

from lovia.http_config import (
    client_options,
    proxy_note,
    resolve_timeout,
    resolve_trust_env,
    resolve_verify,
)

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
    monkeypatch.delenv("LOVIA_HTTP_TRUST_ENV", raising=False)
    assert resolve_trust_env(None) is True


def test_resolve_trust_env_explicit_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOVIA_HTTP_TRUST_ENV", "1")
    assert resolve_trust_env(False) is False
    monkeypatch.setenv("LOVIA_HTTP_TRUST_ENV", "0")
    assert resolve_trust_env(True) is True


def test_resolve_trust_env_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("1", "true", "yes", "on", "ON", "True", ""):
        monkeypatch.setenv("LOVIA_HTTP_TRUST_ENV", val)
        assert resolve_trust_env(None) is True


def test_resolve_trust_env_falsey_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for val in ("0", "false", "no", "off", " OFF "):
        monkeypatch.setenv("LOVIA_HTTP_TRUST_ENV", val)
        assert resolve_trust_env(None) is False


def _route_of(options: dict) -> object | None:
    return (options.get("mounts") or {}).get("all://")


@pytest.fixture
def no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
        "LOVIA_HTTP_TRUST_ENV",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_client_options_carry_both_settings(no_proxy_env: pytest.MonkeyPatch) -> None:
    no_proxy_env.setenv("LOVIA_HTTP_INSECURE", "1")
    # httpx's own proxy reading stays off: it would skip the bypass list.
    assert client_options() == {"trust_env": False, "verify": False}
    no_proxy_env.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    assert _route_of(client_options()) is not None
    assert _route_of(client_options(False)) is None
    # A test's transport is kept, and routes nothing.
    kept = object()
    assert client_options(transport=kept)["transport"] is kept
    assert "mounts" not in client_options(transport=kept)


async def test_the_route_sends_bypassed_and_loopback_hosts_direct(
    no_proxy_env: pytest.MonkeyPatch,
) -> None:
    import httpx

    no_proxy_env.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    no_proxy_env.setenv("NO_PROXY", ".corp.example")
    route = _route_of(client_options())
    assert route is not None
    direct, proxied = route._every[0], route._every[1]  # type: ignore[attr-defined]
    pick = route._pick  # type: ignore[attr-defined]
    assert pick(httpx.URL("https://api.openai.com/v1")) is proxied
    for url in (
        "https://mcp.corp.example/mcp",
        "https://corp.example/",
        "http://localhost:11434/v1",
        "https://127.0.0.1:8443/",
        "https://[::1]/",
    ):
        assert pick(httpx.URL(url)) is direct, url
    # No proxy for plain http here: it goes direct.
    assert pick(httpx.URL("http://plain.example/")) is direct
    await route.aclose()


def test_an_unusable_proxy_is_skipped_not_fatal(
    no_proxy_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    no_proxy_env.setenv("HTTPS_PROXY", "ftp://proxy.corp:21")
    assert _route_of(client_options()) is None
    assert "not using the proxy proxy.corp:21: httpx has no ftp" in caplog.text
    # Nor does a failure's hint blame the proxy that was never used.
    assert proxy_note("https://api.openai.com/v1", True) is None
    # A malformed port is skipped, not raised while saying so.
    no_proxy_env.setenv("HTTPS_PROXY", "http://proxy.corp:port")
    assert _route_of(client_options()) is None
    assert "not using the proxy proxy.corp: a malformed address" in caplog.text
    no_proxy_env.setenv("HTTPS_PROXY", "http://[::1")
    assert _route_of(client_options()) is None
    assert "not using the proxy ?: a malformed address" in caplog.text


def test_a_port_named_in_no_proxy_is_kept(no_proxy_env: pytest.MonkeyPatch) -> None:
    import httpx

    no_proxy_env.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    no_proxy_env.setenv("NO_PROXY", "api.corp.example:8443")
    route = _route_of(client_options())
    pick, (direct, proxied) = route._pick, route._every  # type: ignore[union-attr]
    assert pick(httpx.URL("https://api.corp.example:8443/v1")) is direct
    assert pick(httpx.URL("https://api.corp.example/v1")) is proxied
    assert proxy_note("https://api.corp.example:8443/v1", True) is None


# ----------------------------------------------------------- proxy_note -


@pytest.fixture
def proxied(no_proxy_env: pytest.MonkeyPatch) -> None:
    no_proxy_env.setenv("HTTPS_PROXY", "http://user:secret@proxy.corp:8080")
    no_proxy_env.setenv("NO_PROXY", ".corp.example")


def test_proxy_note_says_the_proxy_was_skipped(proxied: None) -> None:
    note = proxy_note("https://api.openai.com/v1", False)
    assert note is not None
    assert "skipped the proxy proxy.corp:8080" in note
    assert "api.openai.com" in note
    assert "secret" not in note  # never the proxy's credentials


def test_proxy_note_says_the_proxy_was_used(proxied: None) -> None:
    note = proxy_note("https://api.openai.com/v1", True, switch="the System proxy")
    assert note is not None
    assert "went through the proxy proxy.corp:8080" in note
    assert "bypass list" in note and "the System proxy off" in note


def test_proxy_note_is_quiet_when_no_proxy_applies(
    proxied: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bypassed, loopback, or a scheme with no proxy of its own.
    assert proxy_note("https://mcp.corp.example/mcp", True) is None
    assert proxy_note("http://localhost:8000/v1", True) is None
    assert proxy_note("http://plain.example/", True) is None
    monkeypatch.delenv("HTTPS_PROXY")
    assert proxy_note("https://api.openai.com/v1", False) is None
