"""Environment-driven configuration for lovia's outbound ``httpx`` clients.

The model providers and the ``http_fetch`` tool all make HTTPS requests through
``httpx``. This module centralizes how their TLS trust, request timeout, and
proxy behavior are resolved from the environment, so the same knobs apply
everywhere (handy behind an intranet CA or proxy).
"""

from __future__ import annotations

import logging
import os
import ssl

import certifi

__all__ = ["resolve_timeout", "resolve_trust_env", "resolve_verify"]

logger = logging.getLogger(__name__)

_INSECURE_ENV = "LOVIA_HTTP_INSECURE"
_CA_BUNDLE_ENV = "LOVIA_HTTP_CA_BUNDLE"
_PROVIDER_TIMEOUT_ENV = "LOVIA_PROVIDER_TIMEOUT"
_TRUST_ENV_ENV = "LOVIA_PROVIDER_TRUST_ENV"
_DEFAULT_TIMEOUT = 60.0


def resolve_verify() -> ssl.SSLContext | bool:
    """Resolve TLS verification for an outbound ``httpx`` client.

    Priority:

    * ``LOVIA_HTTP_INSECURE=1`` disables certificate verification — use only on
      trusted networks; it exposes the connection to man-in-the-middle attacks.
    * ``LOVIA_HTTP_CA_BUNDLE`` selects a PEM bundle for internal or self-signed
      certificates.
    * the optional ``truststore`` package, when installed, uses the operating
      system trust store — so a CA installed system-wide (what the browser
      already trusts) works with zero configuration. Handy on an intranet.
    * otherwise certifi's bundle is used.

    Applies to both the model providers and the ``http_fetch`` tool. Returns an
    :class:`ssl.SSLContext` (httpx deprecates ``verify=<path string>``) or
    ``False`` to disable verification.
    """
    if os.environ.get(_INSECURE_ENV) == "1":
        return False
    if ca := os.environ.get(_CA_BUNDLE_ENV):
        return ssl.create_default_context(cafile=ca)
    try:
        import truststore
    except ImportError:
        return ssl.create_default_context(cafile=certifi.where())
    context: ssl.SSLContext = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return context


def resolve_timeout(timeout: float | None) -> float:
    """Resolve a provider request timeout in seconds.

    Precedence mirrors how providers source ``base_url``/``api_key``: an
    explicit ``timeout`` argument wins, then the ``LOVIA_PROVIDER_TIMEOUT``
    environment variable, then a 60-second default. A non-numeric or
    non-positive env value is ignored with a warning.
    """
    if timeout is not None:
        return timeout
    raw = os.environ.get(_PROVIDER_TIMEOUT_ENV)
    if not raw:
        return _DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "ignoring invalid %s=%r (not a number)", _PROVIDER_TIMEOUT_ENV, raw
        )
        return _DEFAULT_TIMEOUT
    if value <= 0:
        logger.warning("ignoring non-positive %s=%r", _PROVIDER_TIMEOUT_ENV, raw)
        return _DEFAULT_TIMEOUT
    return value


def resolve_trust_env(trust_env: bool | None) -> bool:
    """Whether the provider HTTP client honors proxy / netrc env settings.

    Explicit argument wins, then ``LOVIA_PROVIDER_TRUST_ENV`` (truthy:
    ``1``/``true``/``yes``/``on``), else ``False``. Enabling it lets httpx pick
    up ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY`` for provider calls.
    """
    if trust_env is not None:
        return trust_env
    raw = os.environ.get(_TRUST_ENV_ENV, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}
