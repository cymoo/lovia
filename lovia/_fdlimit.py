"""Process-wide open-file limits — POSIX only, a quiet no-op elsewhere.

A server running several agents at once holds a lot of descriptors: one
``web_search`` peaks at about a dozen sockets, and every page fetch, provider
stream, SSE client and workspace subprocess costs more on top. macOS still
hands a launchd-started shell a soft limit of 256, low enough for a single
burst of parallel tool calls to exhaust — and none of the resulting failures
names the cause: sqlite3 reports "unable to open database file", DNS resolvers
report having no connections available.

Windows has no equivalent to raise, and needs none: its sockets are Winsock
handles and SQLite goes through Win32, so neither draws on the C runtime's
descriptor table that its limit governs. Both helpers return quietly there.
"""

from __future__ import annotations

import errno
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_SOFT_LIMIT = 8192
"""What :func:`raise_soft_limit` aims for.

Generous next to real demand (a few hundred descriptors at peak) without going
to the kernel's ceiling: a very large ``RLIMIT_NOFILE`` slows the
descriptor-closing path every ``subprocess`` spawn walks, and the workspace
tools spawn plenty.
"""


def raise_soft_limit(target: int = DEFAULT_SOFT_LIMIT) -> None:
    """Raise this process's open-file soft limit toward ``target``.

    For a process that owns its own limits — a CLI, not a library embedded in
    someone else's — so ``lovia web`` calls this at startup while ``serve()``
    does not. A limit that stays low is warned about rather than raised on: it
    degrades into failures under load, which is worth saying out loud, but it
    is no reason to refuse to start.
    """
    try:
        import resource
    except ImportError:  # Windows — see the module docstring
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= target:
        return
    # An unlimited hard cap is normal on macOS; the kernel still enforces
    # kern.maxfilesperproc, far above anything asked for here.
    want = target if hard == resource.RLIM_INFINITY else min(target, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (OSError, ValueError) as exc:
        logger.warning(
            "could not raise the open-file limit above %d (%s) — parallel tool "
            "calls may fail with 'unable to open database file'; raise it in "
            "the shell with: ulimit -n %d",
            soft,
            exc,
            target,
        )
        return
    logger.debug("open-file soft limit raised %d -> %d", soft, want)


def out_of_file_descriptors() -> int | None:
    """This process's soft limit, if it cannot open a file *right now*.

    ``None`` when it can, and on platforms with no such limit. The probe has
    to run while the condition still holds, so callers put it on the failure
    path itself rather than in a diagnostic after the fact.

    Only a per-process shortage (``EMFILE``) answers: a machine-wide one
    (``ENFILE``) is a different problem, and naming the process limit would
    point at the wrong knob.
    """
    try:
        import resource
    except ImportError:  # Windows — see the module docstring
        return None
    try:
        fd = os.open(os.devnull, os.O_RDONLY)
    except OSError as exc:
        if exc.errno == errno.EMFILE:
            soft: int = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
            return soft
        return None
    os.close(fd)
    return None


__all__ = ["DEFAULT_SOFT_LIMIT", "out_of_file_descriptors", "raise_soft_limit"]
