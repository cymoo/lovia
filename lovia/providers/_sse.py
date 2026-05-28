"""Shared SSE parsing helpers for provider adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


async def iter_sse_json(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield JSON payloads from ``data:`` lines in an SSE response.

    Malformed JSON events are ignored to preserve the adapters' historical
    tolerance for provider keep-alives and gateway noise.
    """
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data:
            continue
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event
