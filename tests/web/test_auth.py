"""Token auth for the web API (``lovia.web.auth``)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.auth import TOKEN_COOKIE, generate_token, is_loopback  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402

TOKEN = "sesame-open"


def _client(**kw) -> TestClient:
    agent = Agent(name="bot", model=ScriptedProvider([text("hi")]))
    kw.setdefault("store", ChatStore.in_memory())
    kw.setdefault("generate_titles", False)
    return TestClient(create_app(agent, **kw))


# ------------------------------------------------------------ token mode -


def test_api_requires_token() -> None:
    c = _client(token=TOKEN)
    r = c.get("/api/agents")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    # The detail names the *server* token so the UI's error mapping can tell
    # it apart from a model-provider auth failure.
    assert "server token" in r.json()["detail"]


def test_bearer_header_accepted() -> None:
    c = _client(token=TOKEN)
    r = c.get("/api/agents", headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_cookie_accepted() -> None:
    # <img> previews and download links can't set headers — the UI stores the
    # token in a cookie for those.
    c = _client(token=TOKEN)
    c.cookies.set(TOKEN_COOKIE, TOKEN)
    assert c.get("/api/agents").status_code == 200


def test_wrong_or_malformed_credentials_rejected() -> None:
    c = _client(token=TOKEN)
    assert (
        c.get("/api/agents", headers={"authorization": "Bearer nope"}).status_code
        == 401
    )
    # Wrong scheme, valid token: not accepted as a bearer credential.
    assert (
        c.get("/api/agents", headers={"authorization": f"Basic {TOKEN}"}).status_code
        == 401
    )
    c.cookies.set(TOKEN_COOKIE, "nope")
    assert c.get("/api/agents").status_code == 401


def test_chat_post_and_stream_honor_token() -> None:
    c = _client(token=TOKEN)
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 401
    ok = c.post(
        "/api/chat",
        json={"message": "hi"},
        headers={"authorization": f"Bearer {TOKEN}"},
    )
    assert ok.status_code == 200


def test_healthz_and_ui_shell_stay_open() -> None:
    c = _client(token=TOKEN)
    assert c.get("/healthz").status_code == 200
    # The UI shell is public — it carries no data and must be able to load so
    # it can collect the token client-side.
    assert c.get("/").status_code == 200


def test_no_token_no_auth() -> None:
    c = _client()
    assert c.get("/api/agents").status_code == 200


def test_custom_auth_dependency() -> None:
    async def deny_unless_header(request: Request) -> None:
        if request.headers.get("x-magic") != "yes":
            raise HTTPException(status_code=401, detail="no magic")

    c = _client(auth=deny_unless_header)
    assert c.get("/api/agents").status_code == 401
    assert c.get("/api/agents", headers={"x-magic": "yes"}).status_code == 200


def test_token_and_auth_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        _client(token=TOKEN, auth=lambda: None)


# ------------------------------------------------------------- serve() -


def _fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> dict:
    import uvicorn

    captured: dict = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kw: captured.update(app=app, **kw)
    )
    return captured


def _agent() -> Agent:
    return Agent(name="bot", model=ScriptedProvider([text("hi")]))


def test_serve_generates_token_off_loopback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lovia.web import serve

    captured = _fake_uvicorn(monkeypatch)
    serve(
        _agent(),
        host="0.0.0.0",
        port=1234,
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    out = capsys.readouterr().out
    assert "web API token (generated):" in out
    token = out.split("web API token (generated):", 1)[1].split()[0]

    c = TestClient(captured["app"])
    assert c.get("/api/agents").status_code == 401
    ok = c.get("/api/agents", headers={"authorization": f"Bearer {token}"})
    assert ok.status_code == 200


def test_serve_loopback_stays_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lovia.web import serve

    captured = _fake_uvicorn(monkeypatch)
    serve(_agent(), store=ChatStore.in_memory(), generate_titles=False)
    assert "web API token" not in capsys.readouterr().out
    assert TestClient(captured["app"]).get("/api/agents").status_code == 200


def test_serve_explicit_token_wins_and_is_printed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lovia.web import serve

    captured = _fake_uvicorn(monkeypatch)
    serve(
        _agent(),
        host="0.0.0.0",
        token=TOKEN,
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    out = capsys.readouterr().out
    assert "generated" not in out
    assert f"?token={TOKEN}" in out
    c = TestClient(captured["app"])
    assert (
        c.get("/api/agents", headers={"authorization": f"Bearer {TOKEN}"}).status_code
        == 200
    )


def test_serve_custom_auth_suppresses_generation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from lovia.web import serve

    async def always_deny() -> None:
        raise HTTPException(status_code=401, detail="nope")

    captured = _fake_uvicorn(monkeypatch)
    serve(
        _agent(),
        host="0.0.0.0",
        auth=always_deny,
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    assert "web API token" not in capsys.readouterr().out
    assert TestClient(captured["app"]).get("/api/agents").status_code == 401


# ------------------------------------------------------------- helpers -


def test_is_loopback() -> None:
    for host in ("127.0.0.1", "127.1.2.3", "localhost", "::1"):
        assert is_loopback(host)
    for host in ("0.0.0.0", "::", "192.168.1.10", "example.com"):
        assert not is_loopback(host)


def test_generate_token_is_urlsafe_and_unique() -> None:
    a, b = generate_token(), generate_token()
    assert a != b
    assert len(a) >= 24
    assert all(ch.isalnum() or ch in "-_" for ch in a)
