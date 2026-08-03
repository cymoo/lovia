"""Tests for ``--check`` and the startup summaries (``lovia.web.config``)."""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from lovia.web.config import (  # noqa: E402
    Connection,
    ModelProfile,
    format_app_summary,
    format_summary,
    mask_key,
    run_check,
)


def _conn(model: str = "openai:gpt-5.5", **overrides: object) -> Connection:
    conn = Connection.from_profile(ModelProfile(id="c", model=model))
    for key, value in overrides.items():
        setattr(conn, key, value)
    return conn


def _listing_transport(*ids: str) -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": i} for i in ids]})
    )


# ------------------------------------------------------------- run_check -


def test_run_check_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    rc = run_check(
        _conn(api_key="sk-abcdefghijkl1234"),
        version="1.0",
        out=out,
        transport=_listing_transport("gpt-5.5"),
    )
    assert rc == 0
    text = out.getvalue()
    assert "configuration check" in text
    assert "✓ endpoint reachable" in text
    assert "config files" in text
    assert "./.lovia/config.json (absent)" in text
    assert "~/.lovia/config.json (absent)" in text


def test_run_check_auth_failure_is_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    rc = run_check(
        _conn(api_key="sk-bad"),
        version="1.0",
        out=out,
        transport=httpx.MockTransport(lambda request: httpx.Response(401)),
    )
    assert rc == 2
    assert "✗ authentication failed" in out.getvalue()


def test_run_check_missing_config_is_exit_2_without_probing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    rc = run_check(Connection(), version="1.0", out=out)
    assert rc == 2
    text = out.getvalue()
    assert "missing: model" in text
    # The recovery hint points at the one configuration surface: the web UI.
    assert "Settings" in text


def test_run_check_warns_about_unlisted_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    rc = run_check(
        _conn(model="openai:gpt-5.6", api_key="sk-abcdefghijkl1234"),
        version="1.0",
        out=out,
        transport=_listing_transport("gpt-5.5", "gpt-5.5-mini"),
    )
    assert rc == 0  # warn-only: partial gateway listings never block
    text = out.getvalue()
    assert "does not list 'gpt-5.6'" in text
    assert "close: gpt-5.5" in text


# ---------------------------------------------------------------- summary -


def test_mask_key() -> None:
    assert mask_key(None) == "(none)"
    assert mask_key("short") == "…"
    assert mask_key("sk-abcdefghijkl1234") == "sk-…1234"


def test_format_summary_shows_values_and_config() -> None:
    conn = _conn(api_key="sk-abcdefghijkl1234")
    text = format_summary(
        conn,
        version="0.9.0",
        url="http://127.0.0.1:8000",
        config_desc="~/.lovia/config.json",
        workspace_desc="/work (trusted)",
        db_desc="lovia.db",
    )
    assert "lovia v0.9.0" in text
    assert "openai:gpt-5.5" in text
    assert "sk-…1234" in text
    assert "~/.lovia/config.json" in text
    # gpt-5.5 is in the provider's static table.
    assert "auto (provider reports" in text
    assert text.endswith("serving on http://127.0.0.1:8000")


def test_format_summary_keyless_gateway() -> None:
    conn = Connection.from_profile(
        ModelProfile(id="g", model="mystery", base_url="http://gw/v1")
    )
    text = format_summary(
        conn,
        version="0.9.0",
        url="http://127.0.0.1:9000",
        config_desc="~/.lovia/config.json",
        workspace_desc="(none)",
        db_desc="x.db",
    )
    assert "(none — endpoint does not require one)" in text
    assert "auto (reactive overflow handling)" in text


def test_summary_names_the_env_key_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key left out of the config but present in the provider env is said."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    conn = _conn()  # no api_key stored
    text = format_summary(
        conn,
        version="0.9.0",
        url="http://127.0.0.1:8000",
        config_desc="~/.lovia/config.json",
        workspace_desc="(none)",
        db_desc="x.db",
    )
    assert "OPENAI_API_KEY from the environment applies" in text


def test_format_app_summary() -> None:
    text = format_app_summary(
        version="0.9.0",
        app_target="myagents:assistant",
        db_desc="x.db",
        url="http://127.0.0.1:8000",
    )
    assert "myagents:assistant" in text
    assert text.endswith("serving on http://127.0.0.1:8000")
