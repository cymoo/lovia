"""The stop-condition check a schedule's fire appends to its instruction.

A schedule with a stop condition (``until``) has each fire ask the run itself
to evaluate it and cancel the schedule once met (see
:func:`lovia.web.scheduler.fire_input`). That check is for the agent: it stays
in the transcript the model reads, and :func:`without_stop_check` takes it out
of what the chat shows — the bubble, the live injection, an export — so the
user sees the instruction they wrote.

Stdlib only and import-free of the rest of the web package, so both the
scheduler and the SSE layer (which the scheduler imports) can use it.
"""

from __future__ import annotations

import re

__all__ = ["with_stop_check", "without_stop_check"]

# Matches what with_stop_check writes, whole and only as the message's end —
# so a block a user pastes mid-message, with their own words after it, is
# left alone. Kept word for word: stored transcripts carry this wording.
_CHECK = re.compile(
    r"\n\n\[Fired by schedule ([^\s\]]+)\.\] After the task above, evaluate this "
    r"stop condition: .*?\n"
    r"If it is met, call cancel_schedule\(schedule_id='\1', "
    r"reason=<one line: what you observed>\) and state clearly that the "
    r"schedule is now stopped\. If it is not met, do not call "
    r"cancel_schedule and don't mention the condition\.\Z",
    re.DOTALL,
)


def with_stop_check(text: str, schedule_id: str, until: str) -> str:
    """``text``, then the check for ``until`` addressed to that schedule."""
    return (
        f"{text}\n\n"
        f"[Fired by schedule {schedule_id}.] After the task above, evaluate this "
        f"stop condition: {until}\n"
        f"If it is met, call cancel_schedule(schedule_id='{schedule_id}', "
        f"reason=<one line: what you observed>) and state clearly that the "
        f"schedule is now stopped. If it is not met, do not call "
        f"cancel_schedule and don't mention the condition."
    )


def without_stop_check(text: str) -> str:
    """``text`` as the user wrote it: any check a fire appended, removed."""
    return _CHECK.sub("", text)
