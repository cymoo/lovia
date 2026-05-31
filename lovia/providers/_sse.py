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
    data_lines: list[str] = []

    def parse_buffer() -> dict[str, Any] | None:
        if not data_lines:
            return None
        data = "\n".join(data_lines).strip()
        data_lines.clear()
        if not data or data == "[DONE]":
            return None
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None

    async for line in response.aiter_lines():
        if not line:
            event = parse_buffer()
            if event is not None:
                yield event
            continue
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            break
        data_lines.append(data)

    event = parse_buffer()
    if event is not None:
        yield event
