"""Shared fixtures for the whole test suite."""

from __future__ import annotations

import os

import pytest

from lovia.providers._windows import clear_endpoint_cache


@pytest.fixture(autouse=True)
def _isolate_endpoint_window_cache():
    """Context windows are memoized per endpoint for the life of the process.

    Tests reuse the same ``(base_url, model)`` pairs, so without this a window
    learned or probed in one test would silently answer the next one.
    """
    clear_endpoint_cache()
    yield
    clear_endpoint_cache()


@pytest.fixture(autouse=True)
def _isolate_system_proxy(monkeypatch: pytest.MonkeyPatch):
    """Outbound clients route through the machine's proxy by default: tests
    see only the ``HTTP(S)_PROXY`` a test sets, never the OS proxy of the
    machine running them, and no bypass decision carries over."""
    import urllib.request

    from lovia import http_config

    monkeypatch.setattr(
        urllib.request, "getproxies", urllib.request.getproxies_environment
    )
    monkeypatch.setattr(
        urllib.request, "proxy_bypass", urllib.request.proxy_bypass_environment
    )
    http_config._bypassed.cache_clear()
    http_config._skip.cache_clear()
    # A web test's runtime writes the System proxy into the environment,
    # past monkeypatch: put back what was there.
    switch = os.environ.get("LOVIA_HTTP_TRUST_ENV")
    yield
    if switch is None:
        os.environ.pop("LOVIA_HTTP_TRUST_ENV", None)
    else:
        os.environ["LOVIA_HTTP_TRUST_ENV"] = switch
    http_config._bypassed.cache_clear()
    http_config._skip.cache_clear()
