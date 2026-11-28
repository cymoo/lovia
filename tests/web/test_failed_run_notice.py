"""A run that failed for good keeps no checkpoint, so its error is read back
from its run record: a chat opened afterwards says what the live page said."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from lovia import Agent  # noqa: E402
from lovia.exceptions import ProviderError  # noqa: E402
from lovia.reliability import RetryPolicy  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import text  # noqa: E402
from .test_interrupted_runs import _Flaky, _stream  # noqa: E402
from .test_supervisor import _client, _wait_run  # noqa: E402
from .test_web import _app  # noqa: E402


def _bad_key() -> ProviderError:
    err = ProviderError("HTTP 401: Incorrect API key provided", hint="check the key")
    err.status_code, err.retryable = 401, False
    return err


async def _fail(ac) -> None:
    evs = await _stream(
        ac, "/api/chat/stream", json={"message": "hello", "session_id": "s1"}
    )
    await _wait_run(ac, "s1", gone=True)
    assert [d["code"] for e, d in evs if e == "error"] == ["provider_auth"]


async def test_a_reopened_chat_says_why_its_last_run_failed() -> None:
    store = ChatStore.in_memory()
    agent = Agent(name="bot", model=_Flaky([_bad_key(), text("hi there")]))
    app = _app(agent, store=store, retry=RetryPolicy(max_attempts=1))
    async with _client(app) as ac:
        await _fail(ac)
        detail = (await ac.get("/api/sessions/s1")).json()
        # Nothing to resume or reconnect to: only the error.
        assert detail["active_run_id"] is None and detail["waiting"] is None
        assert detail["last_error"]["code"] == "provider_auth"
        assert "Incorrect API key" in detail["last_error"]["message"]
        assert "check the key" in detail["last_error"]["message"]
        assert [e["role"] for e in detail["entries"]] == ["user"]
        history = (await ac.get("/api/runs/history?session_id=s1")).json()
        assert (history[0]["status"], history[0]["error_code"]) == (
            "failed",
            "provider_auth",
        )

        # A run since then: the failure is no longer the chat's last word.
        evs = await _stream(
            ac, "/api/chat/stream", json={"message": "again", "session_id": "s1"}
        )
        await _wait_run(ac, "s1", gone=True)
        assert [e for e, _ in evs][-1] == "done"
        detail = (await ac.get("/api/sessions/s1")).json()
        assert detail["last_error"] is None


async def test_a_rewind_past_the_failed_run_takes_its_error() -> None:
    store = ChatStore.in_memory()
    agent = Agent(name="bot", model=_Flaky([_bad_key()]))
    app = _app(agent, store=store, retry=RetryPolicy(max_attempts=1))
    async with _client(app) as ac:
        await _fail(ac)
        res = await ac.post("/api/sessions/s1/rewind", json={"user_turn": 0})
        assert res.status_code == 200 and res.json()["entries"] == []
        # The record stays (run history), the notice does not.
        assert (await ac.get("/api/sessions/s1")).json()["last_error"] is None
        history = (await ac.get("/api/runs/history?session_id=s1")).json()
        assert history[0]["status"] == "failed"
