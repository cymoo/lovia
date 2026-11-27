"""Environment-driven configuration for lovia's outbound ``httpx`` clients.

Every HTTP client lovia builds — the model providers, MCP over HTTP, the
fetching tools, embeddings — takes its proxy and TLS options from
:func:`client_options`, so one setting covers every outbound request (handy
behind an intranet CA or proxy). The request timeout
(:func:`resolve_timeout`) is provider-scoped: the tools keep their own.

Proxies route as the machine says, the way a browser does: a host in the
proxy's bypass list (the Windows registry's, macOS's exceptions, or
``NO_PROXY`` beside ``HTTP(S)_PROXY``) and loopback go direct, the rest
through the proxy — so an intranet and the internet both answer
(:func:`resolve_trust_env` turns it off). A PAC file is not read: on a
network configured only by one, requests go direct.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import os
import ssl
import urllib.request
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
_FALSY = frozenset({"0", "false", "no", "off"})


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
    """Whether an outbound HTTP client goes through the machine's proxy.

    Explicit argument wins, then ``LOVIA_HTTP_TRUST_ENV`` (a false value —
    ``0``/``false``/``no``/``off`` — turns it off), else on. On, a request
    goes through ``HTTP(S)_PROXY`` — or, when none is set, the OS proxy —
    unless its host is in the bypass list or loopback.
    """
    if trust_env is not None:
        return trust_env
    return os.environ.get(_TRUST_ENV_ENV, "").strip().lower() not in _FALSY


def client_options(
    trust_env: bool | None = None,
    *,
    lib: Any = None,
    sync: bool = False,
    transport: Any = None,
) -> dict[str, Any]:
    """The proxy and TLS options of every httpx client lovia builds.

    Splat into the constructor —
    ``httpx.AsyncClient(timeout=..., **client_options())`` — so no client can
    take one setting and miss the other. ``trust_env`` as in
    :func:`resolve_trust_env`; ``lib`` is the HTTP library the client comes
    from (httpx unless named — MCP's SDK may use httpx2), ``sync`` for a
    blocking client. A ``transport`` of the caller's (a test's) is kept, and
    routes nothing.
    """
    verify = resolve_verify()
    # httpx's own proxy reading is off: it skips the bypass list.
    options: dict[str, Any] = {"trust_env": False, "verify": verify}
    if transport is not None:
        options["transport"] = transport
    elif resolve_trust_env(trust_env):
        route = _route(lib, verify, sync)
        if route is not None:
            options["mounts"] = {"all://": route}
    return options


def proxy_note(
    url: str,
    trust_env: bool,
    *,
    switch: str = "trust_env (LOVIA_HTTP_TRUST_ENV)",
) -> str | None:
    """How the proxy settings bore on a failed request to ``url``, in a sentence.

    A failure is where a proxy surprise shows — an endpoint reachable only
    through the proxy lovia skipped, or one the proxy cannot reach — and
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
            f"network, add it to the proxy's bypass list (or NO_PROXY), or "
            f"turn {switch} off."
        )
    return (
        f"It skipped the proxy {where} set on this machine, since {switch} is "
        f"off; if {host} is reachable only through that proxy, turn it on."
    )


def _proxies() -> dict[str, str]:
    """The ``http`` and ``https`` proxies: ``HTTP(S)_PROXY``/``ALL_PROXY`` if
    any is set, else the OS's (urllib's reading, as httpx's is)."""
    found = urllib.request.getproxies()
    proxies = {}
    for scheme in ("http", "https"):
        if proxy := found.get(scheme) or found.get("all"):
            proxy = proxy if "://" in proxy else f"http://{proxy}"
            if _usable(proxy):
                proxies[scheme] = proxy
    return proxies


def _usable(proxy: str) -> bool:
    """Whether httpx can send through ``proxy``. One it can't — a malformed
    port, SOCKS4 (Windows' default SOCKS), SOCKS5 without ``socksio`` — is
    left out, once said: requests then go direct, and a failure's hint does
    not blame it."""
    try:
        parts = urlsplit(proxy)
        parts.port  # noqa: B018 — raises on a malformed port
    except ValueError:
        return _skip(proxy, "a malformed address")
    if parts.scheme in ("http", "https"):
        return True
    if parts.scheme in ("socks5", "socks5h"):
        import importlib.util

        if importlib.util.find_spec("socksio") is not None:
            return True
        return _skip(proxy, "SOCKS needs the socksio package")
    return _skip(proxy, f"httpx has no {parts.scheme} proxies")


@functools.cache
def _skip(proxy: str, why: str) -> bool:
    logger.warning("not using the proxy %s: %s", _proxy_address(proxy), why)
    return False


@functools.lru_cache(maxsize=1024)
def _bypassed(host: str, port: int | None = None) -> bool:
    """Whether ``host`` goes direct: loopback always, as a browser does, else
    as the bypass list says. By name, and by ``host:port`` too, which only
    ``NO_PROXY`` entries name (macOS's matching would miss a name with a
    port). Cached: some Pythons look the name up in DNS to decide, which
    would stall the loop on every request — so a changed list needs a
    restart."""
    name = host.strip("[]").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        if ipaddress.ip_address(name).is_loopback:
            return True
    except ValueError:
        pass
    try:
        bypass = urllib.request.proxy_bypass
        return bool(bypass(name) or (port and bypass(f"{name}:{port}")))
    except Exception:  # noqa: BLE001 — an unreadable list bypasses nothing
        return False


def _proxy_for(url: str) -> str | None:
    """The proxy a request to ``url`` goes through with proxies on."""
    parts = urlsplit(url)
    if not parts.hostname or _bypassed(parts.hostname, parts.port):
        return None
    return _proxies().get(parts.scheme)


def _route(lib: Any, verify: ssl.SSLContext | bool, sync: bool) -> Any:
    """A transport sending each request direct or through its scheme's
    proxy, by :func:`_bypassed`; None when the machine names no proxy."""
    if lib is None:
        import httpx as lib
    make = lib.HTTPTransport if sync else lib.AsyncHTTPTransport
    via = {}
    for scheme, proxy in _proxies().items():
        try:
            via[scheme] = make(verify=verify, trust_env=False, proxy=proxy)
        except Exception as exc:  # noqa: BLE001 — what _usable missed: go direct
            _skip(proxy, str(exc))
    if not via:
        return None
    direct = make(verify=verify, trust_env=False)

    def pick(url: Any) -> Any:
        if _bypassed(url.host, url.port):
            return direct
        return via.get(url.scheme, direct)

    return _route_class(lib, sync)(pick, [direct, *via.values()])


@functools.cache
def _route_class(lib: Any, sync: bool) -> Any:
    """The routing transport's class, on ``lib``'s own base class (the
    client enters and closes it as one of its own)."""
    if sync:

        class Route(lib.BaseTransport):  # type: ignore[misc]
            def __init__(self, pick: Any, every: list[Any]) -> None:
                self._pick, self._every = pick, every

            def handle_request(self, request: Any) -> Any:
                return self._pick(request.url).handle_request(request)

            def close(self) -> None:
                for transport in self._every:
                    transport.close()

        return Route

    class AsyncRoute(lib.AsyncBaseTransport):  # type: ignore[misc]
        def __init__(self, pick: Any, every: list[Any]) -> None:
            self._pick, self._every = pick, every

        async def handle_async_request(self, request: Any) -> Any:
            return await self._pick(request.url).handle_async_request(request)

        async def aclose(self) -> None:
            for transport in self._every:
                await transport.aclose()

    return AsyncRoute


def _proxy_address(proxy: str) -> str:
    """``host:port`` of a proxy URL — never its credentials."""
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    host = parts.hostname or "?"
    try:
        port = parts.port
    except ValueError:  # malformed: the name alone
        return host
    return f"{host}:{port}" if port else host
