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

The plugin also exposes ``list_schedules`` and ``cancel_schedule``, which —
unlike ``schedule_run`` — need **no** approval: cancelling only *deactivates*
a schedule (the user can resume or delete it in the panel; hard delete stays
user-only), and the self-cancel path of a stop condition (``until``) must work
inside a clientless scheduled run, where an approval request would park until
the timeout auto-denies it (see ``RunSupervisor._await_approval``).
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
    "## Scheduling\n"
    "You can schedule work for later with the `schedule_run` tool. Use it when "
    "the user wants something to happen at a future time, after a delay, or "
    'repeatedly — e.g. "remind me in 10 minutes", "every morning…", "tomorrow '
    'at 9 check…". Choose the trigger: `at` for a one-time moment, `every` for '
    "a fixed interval in seconds, `cron` for a calendar schedule. For relative "
    'or natural-language times ("in 10 minutes", "tomorrow at 9"), call the '
    "`now` tool first to anchor them (it returns local time with its UTC "
    "offset), then pass an ISO-8601 datetime that keeps that offset. A bare "
    "datetime with no offset is read in the server's own timezone. Write "
    "`instruction` as a complete, standalone prompt (a fire may happen long "
    "after this chat). By default the scheduled run continues THIS conversation "
    "so the user sees results inline; set `continue_session=false` only if the "
    "user asks for it to run in a separate chat. "
    "Don't schedule something you could just do now — and note that creating a "
    "schedule asks the user to approve it first.\n"
    'For "keep doing X until Y" requests, set `until` to the stop condition '
    "and ALWAYS add a safety net — `max_fires` or `expires_at` — sized "
    "generously from the user's intent (e.g. every 60s until a log line "
    "appears → max_fires=120). Each fire is automatically told to evaluate "
    "the condition and cancel the schedule once met. You can also inspect and "
    "stop schedules yourself with `list_schedules` and "
    "`cancel_schedule(schedule_id, reason)` — stopping only deactivates (the "
    "user can resume or delete it in the Scheduled-runs panel), so it needs "
    "no approval; deleting outright is the user's call, via the panel."
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
    # Models often emit a trailing 'Z' for UTC, which datetime.fromisoformat only
    # accepts on Python 3.11+ — normalize it so our 3.10 floor parses it too.
    if expr[-1:] in ("Z", "z"):
        expr = expr[:-1] + "+00:00"
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


def _iso_minutes(epoch: float) -> str:
    """Local ISO-8601 time at minutes precision. astimezone() stamps the
    server's offset so the resolved absolute time is explicit (helps catch a
    timezone the model got wrong)."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="minutes")


def _make_tool(store: ChatStore) -> Tool:
    @tool(name="schedule_run", description=_DESCRIPTION, needs_approval=True)
    async def schedule_run(
        ctx: RunContext[Any],
        instruction: Annotated[
            str,
            "The full, self-contained prompt the scheduled run will execute. "
            "Write it standalone even though it continues this conversation by "
            "default — a fire may happen long after the current context.",
        ],
        trigger_kind: Annotated[
            Literal["at", "every", "cron"],
            "'at' = once at a specific time; 'every' = repeat on a fixed "
            "interval; 'cron' = repeat on a calendar schedule.",
        ],
        trigger_expr: Annotated[
            str,
            "For 'at': an ISO-8601 datetime, ideally with the local UTC offset "
            "(e.g. 2026-06-29T09:00+08:00) — a bare time is read in the server's "
            "timezone; epoch seconds also work. For 'every': the interval in "
            "seconds. For 'cron': a 5-field cron expression like '0 9 * * *'.",
        ],
        continue_session: Annotated[
            bool,
            "If true (the default), each fire continues THIS conversation so its "
            "results land inline here. Set false ONLY when the user explicitly "
            "asks for the scheduled task to run in a separate / new chat.",
        ] = True,
        until: Annotated[
            str | None,
            "Optional stop condition in natural language (e.g. 'the log "
            'contains "ready"\'). Each fire evaluates it after doing the task '
            "and cancels the schedule once it is met. Repeating triggers only; "
            "requires max_fires or expires_at as a safety net.",
        ] = None,
        max_fires: Annotated[
            int | None,
            "Safety net: deactivate the schedule after this many fires, even "
            "if `until` is never met.",
        ] = None,
        expires_at: Annotated[
            str | None,
            "Safety net: deactivate the schedule at this time (same formats "
            "as an 'at' trigger).",
        ] = None,
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

        until_text = until.strip() if until else None
        if until_text and trigger_kind == "at":
            return (
                "A stop condition only makes sense for a repeating trigger "
                "('every' or 'cron') — a one-time 'at' run already stops itself."
            )
        if max_fires is not None and max_fires < 1:
            return "max_fires must be >= 1."
        expires_epoch: float | None = None
        if expires_at is not None and expires_at.strip():
            try:
                expires_epoch = float(_to_epoch(expires_at.strip()))
            except ValueError:
                return (
                    "Couldn't parse expires_at — give an ISO-8601 datetime like "
                    "2026-06-29T09:00 (call the `now` tool first to anchor "
                    "relative times)."
                )
        if until_text and max_fires is None and expires_epoch is None:
            return (
                "A stop condition needs a safety net: also set max_fires or "
                "expires_at so the schedule can't run forever if the condition "
                "is never met."
            )

        # Continue this chat by default so the user sees results inline; the
        # model only opts out (a fresh session per fire) when the user asks.
        # Falls back to a fresh session if this run has no session to continue.
        session_id = ctx.session_id if (continue_session and ctx.session_id) else None
        now = time.time()
        row = ScheduleRow(
            id=uuid.uuid4().hex,
            # The active agent's name — concrete and registered in a real run; if
            # somehow absent, the scheduler resolves its default at fire time.
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
            until=until_text,
            max_fires=max_fires,
            expires_at=expires_epoch,
        )
        await store.add_schedule(row)

        when = _iso_minutes(next_fire)
        scope = "continuing this conversation" if session_id else "as a new chat"
        notes = []
        if until_text:
            notes.append(f"stops when: {until_text}")
        if max_fires is not None:
            notes.append(f"max {max_fires} fires")
        if expires_epoch is not None:
            notes.append(f"expires {_iso_minutes(expires_epoch)}")
        extra = f"; {'; '.join(notes)}" if notes else ""
        return (
            f"Scheduled — first run at {when} ({_describe(trigger_kind, expr)}), "
            f"running {scope}{extra}. The user can manage it in the "
            "Scheduled-runs panel."
        )

    return schedule_run


_LIST_DESCRIPTION = (
    "List the user's scheduled runs: each one's id, instruction, trigger and "
    "status (active / paused / done), plus any stop condition and safety "
    "nets. Call this first when the user refers to a schedule by description "
    "rather than id."
)


def _make_list_tool(store: ChatStore) -> Tool:
    @tool(name="list_schedules", description=_LIST_DESCRIPTION)
    async def list_schedules(ctx: RunContext[Any]) -> str:
        rows = await store.list_schedules()
        if not rows:
            return "No schedules exist."
        lines = []
        for r in rows:
            if r.active:
                status = "active"
            elif r.finished_reason:
                status = f"done ({r.finished_reason})"
            else:
                status = "paused"
            summary = r.input if len(r.input) <= 80 else r.input[:77] + "…"
            line = (
                f"- {r.id}: {summary} — "
                f"{_describe(r.trigger_kind, r.trigger_expr)}, {status}"
            )
            if r.until:
                line += f"; until: {r.until}"
            if r.max_fires is not None:
                line += f"; fires: {r.fire_count}/{r.max_fires}"
            if r.expires_at is not None:
                line += f"; expires: {_iso_minutes(r.expires_at)}"
            lines.append(line)
        return "\n".join(lines)

    return list_schedules


_CANCEL_DESCRIPTION = (
    "Stop a scheduled run. This only deactivates it — the user can resume or "
    "delete it in the Scheduled-runs panel — so no approval is needed. Use it "
    "when a schedule's stop condition is met, or when the user asks for a "
    "schedule to be stopped."
)


def _make_cancel_tool(store: ChatStore) -> Tool:
    @tool(name="cancel_schedule", description=_CANCEL_DESCRIPTION)
    async def cancel_schedule(
        ctx: RunContext[Any],
        schedule_id: Annotated[
            str,
            "The schedule's id — list_schedules shows ids, and a scheduled "
            "run is told its own in its instruction.",
        ],
        reason: Annotated[
            str,
            "One line: why it is being stopped — e.g. the observed stop "
            "condition, or the user's request.",
        ],
    ) -> str:
        row = await store.get_schedule(schedule_id.strip())
        if row is None:
            return (
                f"No schedule with id {schedule_id!r} — call list_schedules "
                "to see current ids."
            )
        if not row.active:
            return f"Schedule {row.id} is already inactive."
        why = reason.strip() or "cancelled by agent"
        await store.set_schedule_active(row.id, active=False, finished_reason=why)
        return (
            f"Schedule {row.id} stopped: {why}. It is deactivated, not "
            "deleted — the user can resume or delete it in the "
            "Scheduled-runs panel."
        )

    return cancel_schedule


@dataclass
class Scheduling:
    """Plugin: tools that let the model create and manage Scheduled runs.

    Construct it with the same :class:`~lovia.web.store.ChatStore` the web app
    uses; ``schedule_run`` writes :class:`~lovia.web.store.ScheduleRow`s the
    :class:`~lovia.web.scheduler.Scheduler` polls, and
    ``list_schedules``/``cancel_schedule`` read and deactivate them. Only
    ``schedule_run`` is gated by approval (see the module docstring for why
    the other two must not be).
    """

    store: ChatStore
    instructions: str | None = _INSTRUCTIONS
    name: str = "scheduling"

    async def setup(self) -> PluginInstance:
        return PluginInstance(
            tools=[
                _make_tool(self.store),
                _make_list_tool(self.store),
                _make_cancel_tool(self.store),
            ],
            instructions=self.instructions,
        )


__all__ = ["Scheduling"]
