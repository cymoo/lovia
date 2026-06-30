"""Time and sleep utility tools.

Both are stateless module-level :class:`Tool` instances::

    from lovia.tools.time import now, sleep
    agent = Agent(name="x", tools=[now, sleep])
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..exceptions import ToolError, UserError
from .base import tool

if TYPE_CHECKING:
    from ..agent import InstructionsFn
    from ..run_context import RunContext

__all__ = ["current_date", "now", "sleep"]


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


def current_date(tz: str | None = None) -> "InstructionsFn":
    """Build an ``@agent.instruction`` fragment that states today's date.

    Register it so the model knows "today" *before* it acts — it then writes the
    current year into web searches and no longer wastes a turn calling ``now``
    first::

        agent = Agent(name="researcher", tools=[duckduckgo_search()])
        agent.instruction(current_date())            # server-local timezone
        # or: agent.instruction(current_date(tz="Asia/Shanghai"))

    Date only, by design: the date is constant within any prompt-cache window,
    so it never meaningfully busts the cache; precise time, when needed, is the
    :func:`now` tool's job. The fragment is re-rendered each run, so it stays
    current across a long-lived session.
    """
    try:
        zone = ZoneInfo(tz) if tz else None
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # Fail at setup with a clear message rather than every render. On Windows
        # the IANA tz database isn't bundled, so even valid names need ``tzdata``.
        raise UserError(
            f"Unknown timezone {tz!r}.",
            hint=(
                "Use an IANA name like 'Asia/Shanghai'. On Windows the IANA tz "
                "database isn't bundled — `pip install tzdata` to enable it."
            ),
        ) from exc

    def fragment(ctx: "RunContext[Any]") -> str:  # ctx required by InstructionsFn
        dt = datetime.now(zone) if zone else datetime.now().astimezone()
        return f"Today's date is {dt:%Y-%m-%d} ({dt:%A})."

    return fragment


@tool
async def sleep(
    seconds: Annotated[float, "How long to sleep, max 60s."],
) -> str:
    """Sleep for ``seconds`` (capped at 60 to avoid runaway calls)."""
    await asyncio.sleep(min(max(seconds, 0.0), 60.0))
    return f"slept {min(max(seconds, 0.0), 60.0)}s"
