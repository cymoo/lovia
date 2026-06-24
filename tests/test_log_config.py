"""Tests for the optional ANSI colorizer used by ``enable_logging()``."""

from __future__ import annotations

import io
import logging

import pytest

import lovia
from lovia.log_config import ColorFormatter, supports_color


def _record(
    *,
    level: int = logging.INFO,
    msg: str = "tool.start: %s",
    args: object = ("grep",),
    name: str = "lovia.x",
) -> logging.LogRecord:
    return logging.LogRecord(name, level, "x.py", 1, msg, args, None)


# ----------------------------------------------------------- ColorFormatter


def test_color_formatter_wraps_level_and_event_token() -> None:
    line = ColorFormatter("%(levelname)-7s %(name)s: %(message)s").format(_record())
    assert "\033[32m" in line  # INFO -> green
    assert "\033[1m" in line  # event token -> bold
    assert "tool.start:" in line  # original text preserved
    assert "grep" in line  # %-args still rendered


def test_color_formatter_restores_record_for_other_handlers() -> None:
    rec = _record()
    ColorFormatter("%(levelname)s %(message)s").format(rec)
    # The record is shared across handlers — it must be left pristine.
    assert rec.levelname == "INFO"
    assert rec.name == "lovia.x"
    assert rec.args == ("grep",)
    assert "\033[" not in str(rec.msg)


def test_color_formatter_levels_are_distinct() -> None:
    fmt = ColorFormatter("%(levelname)s")
    seen = {
        fmt.format(_record(level=level, msg="x", args=None))
        for level in (
            logging.DEBUG,
            logging.INFO,
            logging.WARNING,
            logging.ERROR,
            logging.CRITICAL,
        )
    }
    assert len(seen) == 5  # every level visually distinguishable


def test_color_formatter_pads_level_to_align_columns() -> None:
    # Visible level text is padded to 7 so columns line up like the plain path.
    line = ColorFormatter("%(levelname)s|").format(_record(msg="x", args=None))
    visible = line.replace("\033[32m", "").replace("\033[0m", "")
    assert visible == "INFO   |"


def test_color_formatter_leaves_undotted_prefix_alone() -> None:
    # "memory:" has no dotted event token, so nothing in the message is bolded.
    out = ColorFormatter("%(message)s").format(_record(msg="memory: hi", args=None))
    assert out == "memory: hi"


# ------------------------------------------------------------- supports_color


def test_supports_color_false_without_tty() -> None:
    assert supports_color(io.StringIO()) is False


def test_supports_color_respects_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    assert supports_color(_FakeTTY()) is False


def test_supports_color_respects_dumb_term(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert supports_color(_FakeTTY()) is False


def test_supports_color_enabled_for_plain_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    assert supports_color(_FakeTTY()) is True


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


# --------------------------------------------------- enable_logging(color=...)


@pytest.fixture
def restore_lovia_logger():
    """Snapshot/restore the global ``lovia`` logger that enable_logging mutates."""
    log = logging.getLogger("lovia")
    saved = (list(log.handlers), log.level, log.propagate)
    try:
        yield
    finally:
        log.handlers[:] = saved[0]
        log.setLevel(saved[1])
        log.propagate = saved[2]


def test_enable_logging_color_false_is_plain(restore_lovia_logger: None) -> None:
    buf = io.StringIO()
    lovia.enable_logging(stream=buf, color=False)
    logging.getLogger("lovia.test").info("run.model: turn=1")
    assert "\033[" not in buf.getvalue()


def test_enable_logging_color_true_colorizes(restore_lovia_logger: None) -> None:
    buf = io.StringIO()
    lovia.enable_logging(stream=buf, color=True)
    logging.getLogger("lovia.test").info("run.model: turn=1")
    assert "\033[" in buf.getvalue()


def test_enable_logging_auto_off_for_non_tty(restore_lovia_logger: None) -> None:
    # Default color=None auto-detects: a StringIO isn't a TTY, so plain.
    buf = io.StringIO()
    lovia.enable_logging(stream=buf)
    logging.getLogger("lovia.test").warning("tool.error: boom")
    assert "\033[" not in buf.getvalue()
