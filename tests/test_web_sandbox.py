"""Tests for sandbox-integrated web endpoints (files + audit)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.sandbox import (  # noqa: E402
    AuditStream,
    LocalSandboxProvider,
    attach_sandbox,
)
from lovia.web import create_app  # noqa: E402

from .scripted_provider import ScriptedProvider, text  # noqa: E402


def _build(tmp_path: Path):
    provider = LocalSandboxProvider(root_base=tmp_path)
    audit = AuditStream()
    base = Agent(name="bot", model=ScriptedProvider([text("done"), text("Test Run")]))
    agent = attach_sandbox(base, provider, audit_stream=audit)
    app = create_app(
        agent,
        sandbox_provider=provider,
        audit_stream=audit,
        db_path=tmp_path / "lovia.db",
    )
    return app, provider, audit


async def test_files_endpoint_lists_workspace(tmp_path: Path) -> None:
    app, provider, _ = _build(tmp_path)
    sb = await provider.acquire("sess-files")
    await sb.write("app.py", "print('hi')")
    await sb.write("data.txt", "x")
    c = TestClient(app)
    files = c.get("/api/sessions/sess-files/files").json()
    names = {f["name"] for f in files}
    assert names == {"app.py", "data.txt"}
    await provider.shutdown()


async def test_files_endpoint_reads_file(tmp_path: Path) -> None:
    app, provider, _ = _build(tmp_path)
    sb = await provider.acquire("sess-read")
    await sb.write("notes.md", "# Hello\n")
    c = TestClient(app)
    res = c.get("/api/sessions/sess-read/files/notes.md").json()
    assert res["content"] == "# Hello\n"
    assert res["binary"] is False
    await provider.shutdown()


async def test_files_endpoint_404_on_missing(tmp_path: Path) -> None:
    app, provider, _ = _build(tmp_path)
    await provider.acquire("sess-x")
    c = TestClient(app)
    r = c.get("/api/sessions/sess-x/files/nope.txt")
    assert r.status_code == 404
    await provider.shutdown()


async def test_files_endpoint_handles_binary(tmp_path: Path) -> None:
    app, provider, _ = _build(tmp_path)
    sb = await provider.acquire("sess-bin")
    await sb.write("blob", b"\x00\x01\x02\xff")
    c = TestClient(app)
    res = c.get("/api/sessions/sess-bin/files/blob").json()
    assert res["binary"] is True
    assert "binary" in res["content"]
    await provider.shutdown()


def test_files_endpoint_404_when_no_sandbox_configured(tmp_path: Path) -> None:
    base = Agent(name="bot", model=ScriptedProvider([text("hi")]))
    app = create_app(base)
    c = TestClient(app)
    r = c.get("/api/sessions/anything/files")
    assert r.status_code == 404


async def test_audit_endpoint_returns_per_session_history(tmp_path: Path) -> None:
    app, provider, audit = _build(tmp_path)
    from types import SimpleNamespace

    from lovia.sandbox.audit import AuditRecord

    audit.publish(
        AuditRecord(
            timestamp=1.0,
            session_id="sess-A",
            agent_name="bot",
            tool_name="run",
            command="ls",
            verdict="pass",
            reason="",
        )
    )
    audit.publish(
        AuditRecord(
            timestamp=2.0,
            session_id="sess-B",
            agent_name="bot",
            tool_name="run",
            command="rm -rf /",
            verdict="block",
            reason="dangerous",
        )
    )
    c = TestClient(app)
    a = c.get("/api/sessions/sess-A/audit").json()
    b = c.get("/api/sessions/sess-B/audit").json()
    assert len(a) == 1 and a[0]["command"] == "ls"
    assert len(b) == 1 and b[0]["verdict"] == "block"
    _ = SimpleNamespace  # keep import used


async def test_deleting_chat_releases_sandbox(tmp_path: Path) -> None:
    app, provider, _ = _build(tmp_path)
    sb = await provider.acquire("sess-del")
    await sb.write("a", "1")
    c = TestClient(app)
    c.delete("/api/sessions/sess-del")
    # Sandbox was closed (best-effort); next .read should fail.
    from lovia.sandbox.errors import SandboxClosed

    with pytest.raises(SandboxClosed):
        await sb.read("a")
    await provider.shutdown()


def test_session_rename(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    c = TestClient(app)
    # Create a chat (title gen will run, doesn't matter for this test).
    c.post(
        "/api/chat",
        json={"message": "hi", "session_id": "rename-me"},
    )
    res = c.patch("/api/sessions/rename-me", json={"title": "My Renamed Chat"})
    assert res.status_code == 200
    assert res.json()["title"] == "My Renamed Chat"


def test_list_sessions_after_chat(tmp_path: Path) -> None:
    app, _, _ = _build(tmp_path)
    c = TestClient(app)
    c.post("/api/chat", json={"message": "one"})
    metas = c.get("/api/sessions").json()
    assert len(metas) == 1
