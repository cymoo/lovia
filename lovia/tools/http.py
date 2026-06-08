"""HTTP fetch tool — a thin wrapper around ``httpx`` for one-shot requests.

Already-required dependency, so no extras needed::

    from lovia.tools.http import http_fetch
    agent = Agent(name="x", tools=[http_fetch])
"""

from __future__ import annotations

from typing import Annotated, cast

import httpx

from .._types import JsonObject, JsonValue
from . import tool

__all__ = ["http_fetch"]


@tool
async def http_fetch(
    url: Annotated[str, "Absolute URL to fetch."],
    method: Annotated[str, "HTTP method (GET, POST, ...). Defaults to GET."] = "GET",
    headers: Annotated[dict[str, str] | None, "Optional request headers."] = None,
    body: Annotated[object | None, "Optional JSON body for POST/PUT/PATCH."] = None,
    timeout: Annotated[float, "Request timeout in seconds."] = 30.0,
) -> JsonObject:
    """Fetch a URL and return ``{status, headers, text, json}``.

    The ``json`` field of the result is set only when the response body
    parses as JSON; otherwise it is ``None``. Use this for quick lookups,
    REST API calls, or scraping a single page.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method.upper(), url, headers=headers, json=body)
    parsed: JsonValue = None
    try:
        parsed = cast(JsonValue, resp.json())
    except Exception:  # noqa: BLE001 - body may not be JSON
        parsed = None
    return {
        "status": resp.status_code,
        "headers": dict(resp.headers),
        "text": resp.text,
        "json": parsed,
    }
