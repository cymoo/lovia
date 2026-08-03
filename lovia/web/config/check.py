"""Read-only diagnosis (``--check``) and the startup summary block."""

from __future__ import annotations

import os
import sys
from typing import TextIO

import httpx

from .probe import (
    ValidationOutcome,
    known_context_window,
    unlisted_model_note,
    validate_connection,
)
from .schema import Connection
from .storage import (
    CONFIG_HINT,
    PROJECT_CONFIG_LABEL,
    USER_CONFIG_LABEL,
    project_config_path,
    user_config_path,
)


def mask_key(key: str | None) -> str:
    if not key:
        return "(none)"
    if len(key) > 10:
        return f"{key[:3]}…{key[-4:]}"
    return "…"


def _api_key_cell(conn: Connection) -> str:
    if conn.api_key:
        return mask_key(conn.api_key)
    # The stored config is the single source, with one deliberate exception:
    # a key left out of the file falls back to the provider's own env lookup
    # (a kindness for secret-injection setups) — say so instead of "(none)".
    flavor = conn.flavor
    if flavor is not None:
        var = "ANTHROPIC_API_KEY" if flavor.name == "anthropic" else "OPENAI_API_KEY"
        if os.environ.get(var):
            return f"(none in config — {var} from the environment applies)"
    return "(none — endpoint does not require one)"


def _context_window_cell(conn: Connection) -> str:
    if conn.context_window is not None:
        source = " (from endpoint)" if conn.window_from_endpoint else ""
        return f"{conn.context_window:,}{source}"
    known = known_context_window(conn)
    if known is not None:
        return f"auto (provider reports {known:,})"
    return "auto (reactive overflow handling)"


def _connection_rows(conn: Connection) -> list[tuple[str, str]]:
    """The four connection values, for the startup summary and ``--check``."""
    return [
        ("model", str(conn.model)),
        ("base URL", str(conn.base_url)),
        ("api key", _api_key_cell(conn)),
        ("context window", _context_window_cell(conn)),
    ]


def _config_files_cell() -> str:
    return " · ".join(
        f"{label} ({'present' if path.is_file() else 'absent'})"
        for label, path in (
            (f"./{PROJECT_CONFIG_LABEL}", project_config_path()),
            (USER_CONFIG_LABEL, user_config_path()),
        )
    )


def run_check(
    conn: Connection,
    *,
    version: str,
    out: TextIO | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """``lovia web --check``: report the configuration, probe it, exit.

    Read-only diagnosis — never prompts (no wizard) and never writes. Exit 0
    when the endpoint answered (or can't be verified, which is normal for
    gateways without ``/models``), 2 when configuration is missing or the
    probe failed hard — so CI and containers can assert on it.
    """

    # Resolved at call time, not at import: sys.stdout may be redirected.
    out = out or sys.stdout

    def say(message: str) -> None:
        print(message, file=out)

    say(f"lovia v{version} — configuration check")
    if conn.model is not None:
        for label, value in _connection_rows(conn):
            say(f"  {label:<16} {value}")
    say(f"  {'config files':<16} {_config_files_cell()}")
    missing = conn.missing()
    if missing:
        say(f"  missing: {', '.join(missing)} — {CONFIG_HINT}")
        return 2
    outcome, detail = validate_connection(conn, transport=transport)
    marks = {
        ValidationOutcome.OK: "✓ endpoint reachable",
        ValidationOutcome.AUTH_FAILED: "✗ authentication failed",
        ValidationOutcome.UNREACHABLE: "✗ endpoint unreachable",
        ValidationOutcome.UNVERIFIABLE: "? could not verify the endpoint",
    }
    say(f"  {'probe':<16} {marks[outcome]} ({detail})")
    note = unlisted_model_note(conn)
    if note:
        say(f"  {note}")
    ok = outcome in (ValidationOutcome.OK, ValidationOutcome.UNVERIFIABLE)
    return 0 if ok else 2


def format_summary(
    conn: Connection,
    *,
    version: str,
    url: str,
    config_desc: str,
    workspace_desc: str,
    db_desc: str,
) -> str:
    """The startup block: the connection in play, and where it came from."""
    rows = _connection_rows(conn) + [
        ("config", config_desc),
        ("workspace", workspace_desc),
        ("db", db_desc),
    ]
    lines = [f"lovia v{version}"]
    lines += [f"  {label:<16} {value}" for label, value in rows]
    lines += ["", f"serving on {url}"]
    return "\n".join(lines)


def format_unconfigured_summary(*, version: str, url: str, db_desc: str) -> str:
    """The startup block for a headless launch with nothing configured yet."""
    lines = [
        f"lovia v{version}",
        "  model            (not configured — open the UI to connect one)",
        f"  {'config files':<16} {_config_files_cell()}",
        f"  db               {db_desc}",
        "",
        f"serving on {url}",
    ]
    return "\n".join(lines)


def format_app_summary(*, version: str, app_target: str, db_desc: str, url: str) -> str:
    lines = [
        f"lovia v{version}",
        f"  app              {app_target}",
        f"  db               {db_desc}",
        "",
        f"serving on {url}",
    ]
    return "\n".join(lines)
