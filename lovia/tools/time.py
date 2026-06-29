"""Time and sleep utility tools.

Both are stateless module-level :class:`Tool` instances::

    from lovia.tools.time import now, sleep
    agent = Agent(name="x", tools=[now, sleep])
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..exceptions import ToolError
from .base import tool

__all__ = ["now", "sleep"]


@tool
def now(
    tz: Annotated[
        str | None,
        "IANA timezone name, e.g. 'UTC' or 'Asia/Shanghai'. Defaults to the "
        "server's local timezone.",
    ] = None,
) -> str:
    """Return the current wall-clock time as an ISO-8601 string.

    Defaults to the server's local timezone (the string carries its UTC
    offset); pass ``tz`` for a specific zone.
    """
    if tz is None:
        return datetime.now().astimezone().isoformat()
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # Surface a clear, model-readable error instead of a raw traceback.
        # On Windows the IANA tz database isn't part of the OS, so even valid
        # names like "Asia/Shanghai" fail until ``tzdata`` is installed.
        raise ToolError(
            f"Unknown timezone {tz!r}.",
            hint=(
                "Use an IANA name like 'Asia/Shanghai'. On Windows the IANA tz "
                "database isn't bundled — `pip install tzdata` to enable it."
            ),
        ) from exc
    return datetime.now(zone).isoformat()


@tool
async def sleep(
    seconds: Annotated[float, "How long to sleep, max 60s."],
) -> str:
    """Sleep for ``seconds`` (capped at 60 to avoid runaway calls)."""
    await asyncio.sleep(min(max(seconds, 0.0), 60.0))
    return f"slept {min(max(seconds, 0.0), 60.0)}s"
