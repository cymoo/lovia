"""Time and sleep utility tools.

Both are stateless module-level :class:`Tool` instances::

    from lovia.tools.time import now, sleep
    agent = Agent(name="x", tools=[now, sleep])
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Annotated
from zoneinfo import ZoneInfo

from . import tool

__all__ = ["now", "sleep"]


@tool
def now(
    tz: Annotated[
        str | None, "IANA timezone name, e.g. 'UTC' or 'Asia/Shanghai'."
    ] = None,
) -> str:
    """Return the current wall-clock time as an ISO-8601 string."""
    if tz is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.now(ZoneInfo(tz)).isoformat()


@tool
async def sleep(
    seconds: Annotated[float, "How long to sleep, max 60s."],
) -> str:
    """Sleep for ``seconds`` (capped at 60 to avoid runaway calls)."""
    await asyncio.sleep(min(max(seconds, 0.0), 60.0))
    return f"slept {min(max(seconds, 0.0), 60.0)}s"
