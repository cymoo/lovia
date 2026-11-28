"""Keep the machine from sleeping while there is work for it.

Self-contained: it knows a yes/no question and the operating system, nothing
of runs or settings — the caller's ``wanted`` says when. Only the idle sleep
is held off; closing the lid or putting the machine to sleep still does.

* macOS — a ``caffeinate`` child, told to end with this process.
* Windows — ``SetThreadExecutionState``, which the system drops with the
  process. On a Modern Standby laptop on battery it may still sleep a few
  minutes after the screen turns off.

Elsewhere there is no way that holds on every desktop, so
:func:`supported` is False and :func:`keep_awake` does nothing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Short, so work that starts just before the machine would have slept (a
# scheduled run, a message from another device) is still caught.
POLL_SECONDS = 2.0

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def supported() -> bool:
    """Whether this machine's idle sleep can be held off."""
    if sys.platform == "darwin":
        return shutil.which("caffeinate") is not None
    return sys.platform == "win32"


class _Hold:
    """The hold itself: ``set(True)`` takes it (again, if it was lost),
    ``set(False)`` lets it go. Call from one thread — Windows keeps the
    state per thread."""

    def __init__(self) -> None:
        self._child: subprocess.Popen[bytes] | None = None
        self._on = False

    def set(self, on: bool) -> None:
        if sys.platform == "darwin":
            self._caffeinate(on)
        elif sys.platform == "win32":
            if on != self._on:
                import ctypes

                state = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
                # Zero is a refusal, not an exception.
                if not ctypes.windll.kernel32.SetThreadExecutionState(state):
                    raise OSError("SetThreadExecutionState refused the request")
        self._on = on

    def _caffeinate(self, on: bool) -> None:
        child = self._child
        if not on:
            if child is not None:
                child.terminate()
                child.wait()
                self._child = None
            return
        if child is not None and child.poll() is None:
            return
        # -i: no idle sleep. -w: ends with this process, however it ends.
        self._child = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


async def keep_awake(
    wanted: Callable[[], bool], *, interval: float = POLL_SECONDS
) -> None:
    """Hold off idle sleep whenever ``wanted()`` says so, until cancelled.

    Asked afresh every ``interval`` seconds rather than told of each change:
    nothing to forget to call, and a hold that was lost is taken again.
    """
    if not supported():
        return
    hold = _Hold()
    try:
        while True:
            try:
                hold.set(bool(wanted()))
            except Exception:
                # Staying awake is a nicety: never the reason a server stops.
                logger.warning("keep-awake failed; giving up on it", exc_info=True)
                return
            await asyncio.sleep(interval)
    finally:
        try:
            hold.set(False)
        except Exception:
            logger.debug("keep-awake release failed", exc_info=True)
