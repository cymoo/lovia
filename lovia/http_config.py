"""Environment-driven configuration for lovia's outbound ``httpx`` clients.

Every HTTP client lovia builds — the model providers, MCP over HTTP, the
fetching tools, embeddings — takes its proxy and TLS options from
:func:`client_options`, so one setting covers every outbound request (handy
behind an intranet CA or proxy). The request timeout
(:func:`resolve_timeout`) is provider-scoped: the tools keep their own.

Proxies are off unless asked for (:func:`resolve_trust_env`). httpx reads the
OS proxy (the Windows registry, macOS system settings) but not its bypass
list or PAC file, so honoring it by default would send intranet hosts to a
proxy that cannot reach them.
"""

from __future__ import annotations

import logging
import os
import ssl
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_TIMEOUT",
    "client_options",
    "proxy_note",
    "resolve_timeout",
    "resolve_trust_env",
    "resolve_verify",
]

logger = logging.getLogger(__name__)

_INSECURE_ENV = "LOVIA_HTTP_INSECURE"
_CA_BUNDLE_ENV = "LOVIA_HTTP_CA_BUNDLE"
_PROVIDER_TIMEOUT_ENV = "LOVIA_PROVIDER_TIMEOUT"
_TRUST_ENV_ENV = "LOVIA_HTTP_TRUST_ENV"
DEFAULT_TIMEOUT = 300.0
"""Default provider request timeout in seconds (see :func:`resolve_timeout`)."""
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_truthy(name: str) -> bool:
    """Whether env var ``name`` holds a truthy value (``1``/``true``/``yes``/``on``)."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def resolve_verify() -> ssl.SSLContext | bool:
    """Resolve TLS verification for an outbound ``httpx`` client.

    Priority:

    * ``LOVIA_HTTP_INSECURE`` (truthy: ``1``/``true``/``yes``/``on``) disables
      certificate verification — use only on trusted networks; it exposes the
      connection to man-in-the-middle attacks.
    * ``LOVIA_HTTP_CA_BUNDLE`` selects a PEM bundle for internal or self-signed
      certificates.
    * the optional ``truststore`` package, when installed, uses the operating
      system trust store — so a CA installed system-wide (what the browser
      already trusts) works with zero configuration. Handy on an intranet.
    * otherwise certifi's bundle is used.

    Applies to both the model providers and the fetching tools. Returns an
    :class:`ssl.SSLContext` (httpx deprecates ``verify=<path string>``) or
    ``False`` to disable verification.
    """
    if _env_truthy(_INSECURE_ENV):
        return False
    if ca := os.environ.get(_CA_BUNDLE_ENV):
        return ssl.create_default_context(cafile=ca)
    try:
        import truststore
    except ImportError:
        # certifi reaches us transitively via httpx, not as a declared
        # dependency — import it only on this fallback path so ``import lovia``
        # never hard-requires it.
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    context: ssl.SSLContext = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return context


def resolve_timeout(timeout: float | None) -> float:
    """Resolve a provider request timeout in seconds.

    Precedence mirrors how providers source ``base_url``/``api_key``: an
    explicit ``timeout`` argument wins, then the ``LOVIA_PROVIDER_TIMEOUT``
    environment variable, then :data:`DEFAULT_TIMEOUT` (300 seconds). A
    non-numeric or non-positive env value is ignored with a warning.
    """
    if timeout is not None:
        return timeout
    raw = os.environ.get(_PROVIDER_TIMEOUT_ENV)
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r (not a number)", _PROVIDER_TIMEOUT_ENV, raw
        )
        return DEFAULT_TIMEOUT
    if value <= 0:
        logger.warning("ignoring non-positive %s=%r", _PROVIDER_TIMEOUT_ENV, raw)
        return DEFAULT_TIMEOUT
    return value


def resolve_trust_env(trust_env: bool | None) -> bool:
    """Whether an outbound HTTP client honors proxy / netrc settings.

    Explicit argument wins, then ``LOVIA_HTTP_TRUST_ENV`` (truthy:
    ``1``/``true``/``yes``/``on``), else ``False``. Enabling it lets httpx pick
    up ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY`` — and, when none is set,
    the OS proxy, without its bypass list.
    """
    if trust_env is not None:
        return trust_env
    return _env_truthy(_TRUST_ENV_ENV)


def client_options(trust_env: bool | None = None) -> dict[str, Any]:
    """The proxy and TLS options of every httpx client lovia builds.

    Splat into the constructor —
    ``httpx.AsyncClient(timeout=..., **client_options())`` — so no client can
    take one setting and miss the other. ``trust_env`` as in
    :func:`resolve_trust_env`.
    """
    return {"trust_env": resolve_trust_env(trust_env), "verify": resolve_verify()}


def proxy_note(
    url: str,
    trust_env: bool,
    *,
    switch: str = "trust_env (LOVIA_HTTP_TRUST_ENV)",
) -> str | None:
    """How the proxy settings bore on a failed request to ``url``, in a sentence.

    A failure is where a proxy surprise shows — an endpoint reachable only
    through the proxy lovia skipped, or an intranet host sent to one — and
    nothing else in the error says which. ``None`` when no proxy applies to
    ``url``. ``switch`` names the setting to flip, as the reader knows it.
    """
    try:
        host = urlsplit(url).hostname
        proxy = _proxy_for(url)
        if not host or not proxy:
            return None
        where = _proxy_address(proxy)
    except Exception:  # noqa: BLE001 — a hint must never mask the failure it explains
        return None
    if trust_env:
        return (
            f"It went through the proxy {where}; if {host} is on your own "
            f"network, turn {switch} off or add the host to NO_PROXY."
        )
    return (
        f"It skipped the proxy {where} set on this machine, since {switch} is "
        f"off; if {host} is reachable only through that proxy, turn it on."
    )


def _proxy_for(url: str) -> str | None:
    """The proxy httpx would route ``url`` through with ``trust_env`` on.

    httpx's own rules (its private ``_utils``, so a change there only costs
    the hint): ``NO_PROXY`` matching differs from urllib's in the details —
    ports, an apex domain against ``.domain``.
    """
    import httpx
    from httpx._utils import URLPattern, get_environment_proxies

    target = httpx.URL(url)
    # Most specific first, as httpx sorts its mounts.
    mounts = [
        (URLPattern(key), proxy) for key, proxy in get_environment_proxies().items()
    ]
    for pattern, proxy in sorted(mounts, key=lambda mount: mount[0]):
        if pattern.matches(target):
            return proxy
    return None


def _proxy_address(proxy: str) -> str:
    """``host:port`` of a proxy URL — never its credentials."""
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    host = parts.hostname or "?"
    return f"{host}:{parts.port}" if parts.port else host
