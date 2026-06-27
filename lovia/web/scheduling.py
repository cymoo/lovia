"""Model-driven scheduling: a ``schedule_run`` tool for the default web agent.

Today a Scheduled run can only be created through the UI form
(``POST /api/schedules``). This plugin lets the *model* create one from chat —
"remind me in 10 minutes…", "every morning summarize…", "tomorrow at 9 check…"
— by calling :func:`schedule_run`. The tool writes the same
:class:`~lovia.web.store.ScheduleRow` the
:class:`~lovia.web.scheduler.Scheduler` already polls, reusing the REST handler's
validation helpers (:func:`~lovia.web.scheduler.validate_trigger`,
:func:`~lovia.web.scheduler.initial_next_fire`).

Because a scheduled run executes autonomously later, the tool is gated by
approval (``needs_approval=True``): the model proposes the schedule and the user
approves or denies it inline before it is saved.

The tool needs only the :class:`~lovia.web.store.ChatStore`; everything else
comes from the :class:`~lovia.run_context.RunContext` (the active agent, the
session id). So the plugin closes the tool over the store — the same idiom
:class:`~lovia.plugins.Todo` uses for its list — and no run-path plumbing is
required.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from ..plugins.base import PluginInstance
from ..run_context import RunContext
from ..tools import Tool, tool
from .scheduler import initial_next_fire, validate_trigger
from .store import ChatStore, ScheduleRow

_DESCRIPTION = (
    "Schedule a run to execute later, after a delay, or on a recurring basis. "
    "At each scheduled time the agent runs autonomously with `instruction` as "
    "its prompt. Use this for reminders, periodic checks or summaries, and "
    "deferred tasks — anything the user wants to happen in the future or "
    "repeatedly. Creating a schedule requires the user's approval."
)

_INSTRUCTIONS = (
    "You can schedule work for later with the `schedule_run` tool. Use it when "
    "the user wants something to happen at a future time, after a delay, or "
    'repeatedly — e.g. "remind me in 10 minutes", "every morning…", "tomorrow '
    'at 9 check…". Choose the trigger: `at` for a one-time moment, `every` for '
    "a fixed interval in seconds, `cron` for a calendar schedule. For relative "
    'or natural-language times ("in 10 minutes", "tomorrow at 9"), call the '
    "`now` tool first to anchor them, then pass an ISO-8601 datetime. Write "
    "`instruction` as a complete, standalone prompt: the scheduled run starts "
    "fresh with no memory of this chat unless you set `continue_session=true`. "
    "Don't schedule something you could just do now — and note that creating a "
    "schedule asks the user to approve it first."
)


def _to_epoch(expr: str) -> str:
    """Normalize an ``at`` trigger to an epoch-seconds string.

    Accepts a raw epoch number (passed through) or an ISO-8601 datetime; a naive
    datetime is interpreted in the server's local time zone. Raises
    ``ValueError`` if the value is neither — models reliably produce ISO-8601
    (and have the ``now`` tool to anchor relative times) but not raw epochs.
    """
    try:
        float(expr)
        return expr  # already epoch seconds
    except ValueError:
        pass
    dt = datetime.fromisoformat(expr)  # raises ValueError on anything unparseable
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret a naive datetime as local time
    return str(dt.timestamp())


def _describe(kind: str, expr: str) -> str:
    """A short human-readable phrase for the confirmation message."""
    if kind == "every":
        return f"every {expr}s"
    if kind == "cron":
        return f"cron '{expr}'"
    return "one-time"


def _make_tool(store: ChatStore) -> Tool:
    @tool(name="schedule_run", description=_DESCRIPTION, needs_approval=True)
    async def schedule_run(
        ctx: RunContext[Any],
        instruction: Annotated[
            str,
            "The full, self-contained prompt the scheduled run will execute. It "
            "runs with no prior chat context unless you continue this session.",
        ],
        trigger_kind: Annotated[
            Literal["at", "every", "cron"],
            "'at' = once at a specific time; 'every' = repeat on a fixed "
            "interval; 'cron' = repeat on a calendar schedule.",
        ],
        trigger_expr: Annotated[
            str,
            "For 'at': an ISO-8601 datetime like 2026-06-29T09:00 (or epoch "
            "seconds). For 'every': the interval in seconds. For 'cron': a "
            "5-field cron expression like '0 9 * * *'.",
        ],
        continue_session: Annotated[
            bool,
            "If true, each fire continues THIS conversation; if false (the "
            "default), each fire starts a fresh chat.",
        ] = False,
    ) -> str:
        text = instruction.strip()
        if not text:
            return "Nothing scheduled: the instruction was empty."

        expr = trigger_expr.strip()
        if trigger_kind == "at":
            try:
                expr = _to_epoch(expr)
            except ValueError:
                return (
                    "Couldn't parse the 'at' time — give an ISO-8601 datetime "
                    "like 2026-06-29T09:00 (call the `now` tool first to anchor "
                    "relative times such as 'tomorrow at 9')."
                )
        try:
            validate_trigger(trigger_kind, expr)
            next_fire = initial_next_fire(trigger_kind, expr, now=time.time())
        except (ValueError, RuntimeError) as exc:
            # RuntimeError = croniter not installed (cron triggers are opt-in).
            return f"Couldn't schedule that: {exc}"

        # A fresh session per fire unless the model asked to continue this chat
        # (and this run actually has a session to continue).
        session_id = ctx.session_id if (continue_session and ctx.session_id) else None
        now = time.time()
        row = ScheduleRow(
            id=uuid.uuid4().hex,
            # Pin to the running agent (always a concrete, registered name) so
            # the scheduler never has to fall back to an ambiguous default.
            agent=ctx.agent.name if ctx.agent is not None else None,
            input=text,
            session_id=session_id,
            trigger_kind=trigger_kind,
            trigger_expr=expr,
            next_fire=next_fire,
            active=True,
            last_session_id=None,
            created_at=now,
            updated_at=now,
        )
        await store.add_schedule(row)

        when = datetime.fromtimestamp(next_fire).isoformat(timespec="minutes")
        scope = "continuing this conversation" if session_id else "as a new chat"
        return (
            f"Scheduled — first run at {when} ({_describe(trigger_kind, expr)}), "
            f"running {scope}. The user can manage it in the Scheduled-runs panel."
        )

    return schedule_run


@dataclass
class Scheduling:
    """Plugin: a ``schedule_run`` tool that lets the model create Scheduled runs.

    Construct it with the same :class:`~lovia.web.store.ChatStore` the web app
    uses; the tool writes :class:`~lovia.web.store.ScheduleRow`s the
    :class:`~lovia.web.scheduler.Scheduler` polls. The tool is gated by approval
    (``needs_approval=True``).
    """

    store: ChatStore
    instructions: str | None = _INSTRUCTIONS
    name: str = "scheduling"

    async def setup(self) -> PluginInstance:
        return PluginInstance(
            tools=[_make_tool(self.store)],
            instructions=self.instructions,
        )


__all__ = ["Scheduling"]
