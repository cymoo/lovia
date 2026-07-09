"""Shared fixtures for the whole test suite."""

from __future__ import annotations

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
