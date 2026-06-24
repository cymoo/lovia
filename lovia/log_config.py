"""Logging configuration for lovia: the opt-in console helper and its
optional ANSI colorizer.

Library best practice: a :class:`~logging.NullHandler` is attached to the
``lovia`` logger at import time, so applications that don't configure logging
see neither ``No handlers could be found`` warnings nor unsolicited output.
Users opt in via :func:`enable_logging` or by configuring :mod:`logging`
themselves.

The colorizer is pure stdlib (no dependencies) and is wired in only by
:func:`enable_logging`; the library's own ``logging`` calls stay plain, so
production, piped, and file output are never colorized.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import TextIO

# Attach a NullHandler at import time — see the module docstring.
logging.getLogger("lovia").addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# enable_logging
# ---------------------------------------------------------------------------


def enable_logging(
    level: int | str = logging.INFO,
    *,
    format: str = "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt: str = "%H:%M:%S",
    stream: TextIO | None = None,
    color: bool | None = None,
    propagate: bool = False,
) -> logging.Logger:
    """Configure the ``lovia`` logger for quick interactive use.

    Convenience for scripts and notebooks. Attaches a single
    :class:`~logging.StreamHandler` to the ``lovia`` logger with a sensible
    default format and sets its level. Idempotent — calling more than once
    replaces the previously attached handler so log lines aren't duplicated.

    By default the ``lovia`` logger's :attr:`~logging.Logger.propagate` is set
    to ``False`` so records aren't *also* emitted by the root logger — which
    would double-print whenever the app has configured root logging (e.g. via
    :func:`logging.basicConfig` or under uvicorn). Pass ``propagate=True`` to
    keep propagating to ancestor handlers as well.

    For production deployments configure :mod:`logging` yourself; nothing in
    ``lovia`` calls this function automatically.

    Args:
        level: Logger level (e.g. ``logging.DEBUG``, ``"INFO"``).
        format: ``logging`` format string.
        datefmt: ``logging`` date format string.
        stream: Optional stream override (defaults to ``sys.stderr``).
        color: Colorize the output (level names plus the ``area.event:`` token
            of each message). ``None`` (default) auto-detects: on when the
            stream is a TTY and ``NO_COLOR``/``TERM=dumb`` are unset, off when
            piped or redirected. Pass ``True``/``False`` to force it. Only this
            convenience handler is affected — the library's own log records are
            never colorized.
        propagate: Whether the ``lovia`` logger should also forward records to
            ancestor (root) handlers. Defaults to ``False`` to avoid duplicate
            output.

    Returns:
        The configured ``lovia`` logger.
    """
    log = logging.getLogger("lovia")
    # Strip only the handlers we attached on a previous call, so successive
    # calls don't pile up duplicate StreamHandlers. The NullHandler and any
    # handlers the user added themselves are left untouched.
    for h in list(log.handlers):
        if getattr(h, "_lovia_managed", False):
            log.removeHandler(h)
    handler = logging.StreamHandler(stream)
    if color is None:
        color = supports_color(stream if stream is not None else sys.stderr)
    if color:
        handler.setFormatter(ColorFormatter(format, datefmt=datefmt))
    else:
        handler.setFormatter(logging.Formatter(format, datefmt=datefmt))
    handler._lovia_managed = True  # type: ignore[attr-defined]
    log.addHandler(handler)
    log.setLevel(level)
    log.propagate = propagate
    return log


# ---------------------------------------------------------------------------
# ANSI colorizer (used only by enable_logging)
# ---------------------------------------------------------------------------

# Minimal 8-colour ANSI palette — the widely-supported subset.
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_LEVEL_COLORS = {
    logging.DEBUG: "\033[2m",  # dim
    logging.INFO: "\033[32m",  # green
    logging.WARNING: "\033[33m",  # yellow
    logging.ERROR: "\033[31m",  # red
    logging.CRITICAL: "\033[1;31m",  # bold red
}

# Width the level name is padded to before colouring, matching the ``-7s`` in
# the default format so coloured columns line up exactly as the plain ones do
# (ANSI escapes are invisible but would otherwise count toward the format
# string's field width and break alignment).
_LEVEL_WIDTH = 7

# Leading ``area.event:`` token (e.g. ``tool.start:``, ``memory:``), bolded so it
# stands out in a dense stream. The ``[a-z]`` start leaves ordinary prose alone.
_EVENT_RE = re.compile(r"^([a-z][a-z0-9_.]*:)")


class ColorFormatter(logging.Formatter):
    """Wraps the level name, logger name, and leading ``area.event:`` token in
    ANSI color, then restores the record so other handlers see it untouched."""

    def format(self, record: logging.LogRecord) -> str:
        orig_level = record.levelname
        orig_name = record.name
        orig_msg = record.msg
        orig_args = record.args

        rendered = record.getMessage()
        color = _LEVEL_COLORS.get(record.levelno, "")
        padded = f"{orig_level:<{_LEVEL_WIDTH}}"
        record.levelname = f"{color}{padded}{_RESET}" if color else padded
        record.name = f"{_DIM}{orig_name}{_RESET}"
        # The message is now fully rendered; bold its event token and stop the
        # parent from %-formatting it again (args cleared).
        record.msg = _EVENT_RE.sub(f"{_BOLD}\\1{_RESET}", rendered)
        record.args = None
        try:
            return super().format(record)
        finally:
            # The record is shared across handlers — leave no ANSI behind.
            record.levelname = orig_level
            record.name = orig_name
            record.msg = orig_msg
            record.args = orig_args


def supports_color(stream: TextIO) -> bool:
    """Best-effort: whether ANSI color is appropriate for ``stream``.

    Honors the ``NO_COLOR`` convention and ``TERM=dumb``, requires a TTY, and
    on Windows enables VT processing first (returning ``False`` if that fails).
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
    except Exception:
        return False
    if sys.platform == "win32":
        return _enable_windows_vt()
    return True


def _enable_windows_vt() -> bool:
    """Enable ANSI VT processing on the Windows console, dependency-free.

    ``os.system("")`` initializes the console's virtual-terminal mode on
    Windows 10+ as a side effect; if it fails we skip color rather than print
    raw escape codes.
    """
    try:
        os.system("")
        return True
    except Exception:
        return False
