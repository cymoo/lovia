"""Remembered approvals ("always allow" grants): store rows, the registry's
claim semantics, and the HTTP flow end to end."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip("fastapi")

from lovia import Agent, Verdict, events, tool  # noqa: E402
from lovia.approvals import Approval  # noqa: E402
from lovia.messages import ToolCall  # noqa: E402
from lovia.web import ChatStore, create_app  # noqa: E402
from lovia.web.approvals import ApprovalRegistry  # noqa: E402
from lovia.web.store import grant_context  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_supervisor import _parse_sse, _spawn, _wait_run  # noqa: E402

# ---------------------------------------------------------------- store ---


async def test_grants_roundtrip_and_delete() -> None:
    store = ChatStore.in_memory()
    ctx = grant_context("bot", "/ws")
    await store.add_grants("s1", ["shell:git push"], ctx)
    await store.add_grants("s1", ["shell:git push", "read:~/notes"], ctx)  # idempotent
    assert await store.has_grants("s1", ["shell:git push"], ctx)
    assert await store.has_grants("s1", ["read:~/notes", "shell:git push"], ctx)
    # Every scope must be granted; none at all is never a grant.
    assert not await store.has_grants("s1", ["shell:git push", "shell:make"], ctx)
    assert not await store.has_grants("s1", [], ctx)
    assert not await store.has_grants(
        "s1", ["shell:git push"], grant_context("bot", "/x")
    )
    assert not await store.has_grants("s2", ["shell:git push"], ctx)  # per chat
    assert [g.scope for g in await store.list_grants("s1")] == [
        "shell:git push",
        "read:~/notes",
    ]
    assert await store.delete_grant("s1", "shell:git push", ctx) is True
    assert await store.delete_grant("s1", "shell:git push", ctx) is False
    assert not await store.has_grants("s1", ["shell:git push"], ctx)
    assert not await store.has_grants("s1", ["shell:git push", "read:~/notes"], ctx)


async def test_deleting_a_chat_drops_its_grants() -> None:
    store = ChatStore.in_memory()
    await store.upsert("s1", agent="bot")
    await store.add_grants("s1", ["shell:make"], grant_context("bot", "/ws"))
    await store.delete("s1")
    assert await store.list_grants("s1") == []


async def test_grants_table_reaches_an_existing_database(tmp_path: Path) -> None:
    # A database from before the migration gets the table through the step,
    # not the version-0 schema.
    import sqlite3

    path = tmp_path / "old.db"
    store = ChatStore.sqlite(path)
    await store.upsert("s1", agent="bot")
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE chat_grants")
    conn.execute("UPDATE lovia_schema SET version = 3 WHERE store = 'chat'")
    conn.commit()
    conn.close()
    reopened = ChatStore.sqlite(path)
    await reopened.add_grants("s1", ["shell:make"], grant_context("bot", "/ws"))
    assert len(await reopened.list_grants("s1")) == 1


def test_grant_context_is_unambiguous() -> None:
    # JSON-encoded, so an agent name containing the would-be delimiter can't
    # collide with another (agent, root) pair.
    a = grant_context("bot@/x", None)
    b = grant_context("bot", "/x")
    assert a != b and json.loads(a) == ["bot@/x", None]


# ------------------------------------------------------------- registry ---


def _request(
    call_id: str = "c1", scopes: tuple[str, ...] = ("shell:x",)
) -> events.ApprovalRequired:
    return events.ApprovalRequired(
        call=ToolCall(id=call_id, name="shell", arguments="{}"), scopes=scopes
    )


async def test_claim_owns_the_decision() -> None:
    reg = ApprovalRegistry()
    ev = _request()
    approved: list[bool] = []

    class _Ev(events.ApprovalRequired):
        def approve(self) -> None:  # type: ignore[override]
            approved.append(True)

        def reject(self, reason: str = "") -> None:  # type: ignore[override]
            approved.append(False)

    ev = _Ev(call=ev.call, scopes=ev.scopes)
    waiter = asyncio.create_task(reg.await_decision("s", ev))
    await asyncio.sleep(0)
    assert reg.pending("s", "c1") is ev
    assert reg.claim("s", "c1") is True
    assert reg.claim("s", "c1") is False  # a second request is too late
    # The sweeps leave a claimed approval to its owner — and say so.
    assert reg.deny_pending("s", reason="approval timed out") is False
    await asyncio.sleep(0)
    assert not waiter.done()
    assert await reg.resolve("s", "c1", Approval(True)) is True
    assert await waiter == Approval(True)
    assert approved == [True]
    assert reg.claim("s", "c1") is False  # gone once decided


async def test_unclaimed_approvals_are_swept() -> None:
    reg = ApprovalRegistry()
    ev = _request()
    seen: list[str] = []

    class _Ev(events.ApprovalRequired):
        def approve(self) -> None:  # type: ignore[override]
            seen.append("approve")

        def reject(self, reason: str = "") -> None:  # type: ignore[override]
            seen.append(reason)

    waiter = asyncio.create_task(reg.await_decision("s", _Ev(call=ev.call)))
    await asyncio.sleep(0)
    assert reg.deny_pending("s", reason="approval timed out") is True
    assert await waiter == Approval(False, "approval timed out")
    assert seen == ["approval timed out"]


# -------------------------------------------------------------- HTTP flow ---


def _app(agent: Agent, **kw):
    kw.setdefault("titles", False)
    kw.setdefault("store", ChatStore.in_memory())
    return create_app(agent, **kw)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _agent(tmp_path: Path, *steps) -> Agent:
    @tool(permission=Verdict("ask", "Pushes to a remote.", ("shell:git push",)))
    async def push() -> str:
        return "pushed"

    return Agent(
        name="bot",
        model=ScriptedProvider(list(steps)),
        tools=[push],
        workspace=Workspace.local(str(tmp_path), mode="readonly"),
    )


async def _run_until_approval(ac, sid: str):
    task, lines = _spawn(
        ac, "/api/chat/stream", json={"message": "go", "session_id": sid}
    )
    await _wait_run(ac, sid, status="blocked_on_approval")
    return task, lines


# ``httpx.ASGITransport`` buffers a response whole, so a stream's frames are
# readable only once the run has ended: assert on them after the task.
def _frames(lines: list[str], kind: str) -> list[dict]:
    return [d for e, d in _parse_sse("\n".join(lines)) if e == kind]


@pytest.mark.asyncio
async def test_remembered_approval_answers_the_next_call(tmp_path: Path) -> None:
    store = ChatStore.in_memory()
    agent = _agent(
        tmp_path,
        call("push", {}, call_id="c1"),
        text("first"),
        call("push", {}, call_id="c2"),
        text("second"),
    )
    async with _client(_app(agent, store=store)) as ac:
        task, lines = await _run_until_approval(ac, "s1")
        r = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "approve",
                "remember": True,
            },
        )
        assert r.status_code == 200 and r.json() == {"ok": True, "remembered": True}
        await asyncio.wait_for(task, timeout=5)
        await _wait_run(ac, "s1", gone=True)
        (req,) = _frames(lines, "approval_required")
        assert req["reason"] == "Pushes to a remote."
        assert req["scopes"] == ["shell:git push"]
        assert req["granted"] is False
        (done,) = _frames(lines, "approval_resolved")
        assert done == {"id": "c1", "approved": True, "cause": "user"}

        grants = (await ac.get("/api/sessions/s1/grants")).json()
        assert [(g["scope"], g["active"]) for g in grants] == [("shell:git push", True)]

        # Second turn: the same scope is answered on the server, as a note.
        task2, lines2 = _spawn(
            ac, "/api/chat/stream", json={"message": "again", "session_id": "s1"}
        )
        await asyncio.wait_for(task2, timeout=5)
        (granted,) = _frames(lines2, "approval_required")
        assert granted["granted"] is True and granted["id"] == "c2"
        assert _frames(lines2, "approval_resolved") == []  # no card to settle
        assert any(
            "pushed" in str(d.get("result")) for d in _frames(lines2, "tool_result")
        )

        # Revoked: the next call asks again.
        g = grants[0]
        r = await ac.delete(
            "/api/sessions/s1/grants",
            params={"scope": g["scope"], "context": g["context"]},
        )
        assert r.json() == {"removed": True}
        assert (await ac.get("/api/sessions/s1/grants")).json() == []


@pytest.mark.asyncio
async def test_grant_from_another_chat_or_workspace_does_not_apply(
    tmp_path: Path,
) -> None:
    store = ChatStore.in_memory()
    agent = _agent(tmp_path, call("push", {}, call_id="c1"), text("ok"))
    # Same scope, but remembered for another chat / under another root.
    await store.add_grants("other", ["shell:git push"], grant_context("bot", tmp_path))
    await store.add_grants(
        "s1", ["shell:git push"], grant_context("bot", tmp_path / "else")
    )
    async with _client(_app(agent, store=store)) as ac:
        task, lines = await _run_until_approval(ac, "s1")  # still asks
        listed = (await ac.get("/api/sessions/s1/grants")).json()
        assert [g["active"] for g in listed] == [False]
        await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "deny"},
        )
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_deny_with_reason_reaches_the_model(tmp_path: Path) -> None:
    agent = _agent(tmp_path, call("push", {}, call_id="c1"), text("ok"))
    async with _client(_app(agent)) as ac:
        task, lines = await _run_until_approval(ac, "s1")
        r = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "deny",
                "reason": "open a PR instead",
            },
        )
        assert r.json() == {"ok": True, "remembered": False}
        # A second decision for the same call finds nothing to decide.
        again = await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c1", "decision": "approve"},
        )
        assert again.status_code == 404
        await asyncio.wait_for(task, timeout=5)
    (result,) = _frames(lines, "tool_result")
    assert result["result"] == "Tool push was not approved: open a PR instead"
    (done,) = _frames(lines, "approval_resolved")
    assert done == {"id": "c1", "approved": False, "cause": "user"}


def _chained_agent(tmp_path: Path, *steps) -> Agent:
    @tool(permission=Verdict("ask", "", ("shell:pwd", "shell:python", "shell:git")))
    async def chain() -> str:
        return "ran"

    return Agent(
        name="bot",
        model=ScriptedProvider(list(steps)),
        tools=[chain],
        workspace=Workspace.local(str(tmp_path), mode="readonly"),
    )


@pytest.mark.asyncio
async def test_remember_keeps_only_the_picked_scopes(tmp_path: Path) -> None:
    # The user leaves `python` out: the call still runs, only the others are
    # remembered — so a later call needing all three asks again.
    store = ChatStore.in_memory()
    agent = _chained_agent(
        tmp_path,
        call("chain", {}, call_id="c1"),
        text("first"),
        call("chain", {}, call_id="c2"),
        text("second"),
    )
    async with _client(_app(agent, store=store)) as ac:
        task, lines = await _run_until_approval(ac, "s1")
        r = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "approve",
                "remember": True,
                "scopes": ["shell:git", "shell:pwd"],
            },
        )
        assert r.json() == {"ok": True, "remembered": True}
        await asyncio.wait_for(task, timeout=5)
        await _wait_run(ac, "s1", gone=True)
        assert any("ran" in str(d.get("result")) for d in _frames(lines, "tool_result"))
        # python left out.
        assert {g.scope for g in await store.list_grants("s1")} == {
            "shell:pwd",
            "shell:git",
        }
        # Not every scope is granted: the next call asks again.
        task2, _ = await _run_until_approval(ac, "s1")
        await ac.post(
            "/api/chat/approve",
            json={"session_id": "s1", "call_id": "c2", "decision": "deny"},
        )
        await asyncio.wait_for(task2, timeout=5)


@pytest.mark.asyncio
async def test_remember_rejects_a_scope_the_request_did_not_offer(
    tmp_path: Path,
) -> None:
    # A client can only narrow: naming anything else is a 400, checked before
    # the claim — the approval is still pending and can be answered properly.
    store = ChatStore.in_memory()
    agent = _chained_agent(tmp_path, call("chain", {}, call_id="c1"), text("ok"))
    async with _client(_app(agent, store=store)) as ac:
        task, _ = await _run_until_approval(ac, "s1")
        bad = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "approve",
                "remember": True,
                "scopes": ["shell:pwd", "shell:rm"],
            },
        )
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "invalid_request"
        assert await store.list_grants("s1") == []
        ok = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "approve",
                "remember": True,
                "scopes": [],
            },
        )
        # An empty pick approves and remembers nothing.
        assert ok.json() == {"ok": True, "remembered": False}
        await asyncio.wait_for(task, timeout=5)
        assert await store.list_grants("s1") == []


@pytest.mark.asyncio
async def test_remember_without_scope_approves_but_saves_nothing(
    tmp_path: Path,
) -> None:
    @tool(permission="ask")
    async def plain() -> str:
        return "ran"

    agent = Agent(
        name="bot",
        model=ScriptedProvider([call("plain", {}, call_id="c1"), text("ok")]),
        tools=[plain],
    )
    async with _client(_app(agent)) as ac:
        task, lines = await _run_until_approval(ac, "s1")
        r = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "approve",
                "remember": True,
            },
        )
        assert r.json() == {"ok": True, "remembered": False}
        await asyncio.wait_for(task, timeout=5)
        (req,) = _frames(lines, "approval_required")
        assert req["scopes"] == [] and req["reason"] == ""
        assert (await ac.get("/api/sessions/s1/grants")).json() == []
    assert any("ran" in str(d.get("result")) for d in _frames(lines, "tool_result"))


@pytest.mark.asyncio
async def test_grant_context_follows_the_active_agent_after_a_handoff(
    tmp_path: Path,
) -> None:
    # Entry agent "alpha" in /a hands off to "b" in /b, where the gated tool
    # is called. A grant given to alpha must not answer b's call, and the
    # grant b's approval saves belongs to b — inert once alpha is idle again.
    from lovia import Handoff

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()

    @tool(permission=Verdict("ask", "Pushes to a remote.", ("shell:git push",)))
    async def push() -> str:
        return "pushed"

    b = Agent(
        name="b",
        model=ScriptedProvider([call("push", {}, call_id="c1"), text("b done")]),
        tools=[push],
        workspace=Workspace.local(str(tmp_path / "b"), mode="readonly"),
    )
    alpha = Agent(
        name="bot",
        model=ScriptedProvider([call("transfer_to_b", {}, call_id="h1")]),
        handoffs=[Handoff(target=b)],
        workspace=Workspace.local(str(tmp_path / "a"), mode="readonly"),
    )
    store = ChatStore.in_memory()
    await store.add_grants(
        "s1", ["shell:git push"], grant_context("alpha", tmp_path / "a")
    )
    async with _client(_app({"alpha": alpha}, store=store)) as ac:
        task, lines = await _run_until_approval(ac, "s1")  # alpha's grant: not b's
        r = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "approve",
                "remember": True,
            },
        )
        assert r.json() == {"ok": True, "remembered": True}
        await asyncio.wait_for(task, timeout=5)
        await _wait_run(ac, "s1", gone=True)
        contexts = {g.context for g in await store.list_grants("s1")}
        assert grant_context("b", tmp_path / "b") in contexts
        # Idle, the chat is alpha's again: b's grant is listed but inactive.
        listed = (await ac.get("/api/sessions/s1/grants")).json()
        assert {(g["context"], g["active"]) for g in listed} == {
            (grant_context("alpha", tmp_path / "a"), True),
            (grant_context("b", tmp_path / "b"), False),
        }


@pytest.mark.asyncio
async def test_grant_context_uses_the_registry_key_not_the_internal_name(
    tmp_path: Path,
) -> None:
    # Two agents served as "alpha" and "beta" may both be named "bot" inside
    # and share a root: their grants must not mix, and the context saved by a
    # run must match what the idle route computes (the workspace binding
    # copies the agent, so the key is resolved before that copy is made).
    agent = _agent(tmp_path, call("push", {}, call_id="c1"), text("ok"))
    other = _agent(tmp_path, text("never"))
    store = ChatStore.in_memory()
    await store.add_grants("s1", ["shell:git push"], grant_context("beta", tmp_path))
    app = _app({"alpha": agent, "beta": other}, store=store)
    async with _client(app) as ac:
        # Explicitly alpha's chat; beta's grant must not answer it.
        task, lines = _spawn(
            ac,
            "/api/chat/stream",
            json={"message": "go", "session_id": "s1", "agent": "alpha"},
        )
        await _wait_run(ac, "s1", status="blocked_on_approval")
        r = await ac.post(
            "/api/chat/approve",
            json={
                "session_id": "s1",
                "call_id": "c1",
                "decision": "approve",
                "remember": True,
            },
        )
        assert r.json() == {"ok": True, "remembered": True}
        await asyncio.wait_for(task, timeout=5)
        await _wait_run(ac, "s1", gone=True)
        listed = (await ac.get("/api/sessions/s1/grants")).json()
        assert {(g["context"], g["active"]) for g in listed} == {
            (grant_context("alpha", tmp_path), True),
            (grant_context("beta", tmp_path), False),
        }


@pytest.mark.asyncio
async def test_cancel_during_the_grant_lookup_does_not_park_the_run(
    tmp_path: Path,
) -> None:
    # No approval timeout: a stop that lands while the server is still asking
    # the store about grants — before the approval is registered — must still
    # end the run instead of leaving it waiting for a decision nobody can give.
    store = ChatStore.in_memory()
    agent = _agent(tmp_path, call("push", {}, call_id="c1"), text("ok"))
    looking = asyncio.Event()
    release = asyncio.Event()
    orig_has = store.has_grants

    async def slow_has(*args, **kwargs):
        looking.set()
        await release.wait()
        return await orig_has(*args, **kwargs)

    store.has_grants = slow_has  # type: ignore[method-assign]
    async with _client(_app(agent, store=store)) as ac:
        task, lines = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s1"}
        )
        await asyncio.wait_for(looking.wait(), timeout=5)
        await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        release.set()
        await asyncio.wait_for(task, timeout=5)
        await _wait_run(ac, "s1", gone=True)
    assert not any(
        "pushed" in str(d.get("result")) for d in _frames(lines, "tool_result")
    )


@pytest.mark.asyncio
async def test_cancel_during_a_claimed_approval_keeps_the_grant(tmp_path: Path) -> None:
    # The claim is the moment the user's decision is accepted: a cancel that
    # lands while the grant is being written still stops the run, but the
    # grant stays — it was a decision the user made, not a matter of timing.
    store = ChatStore.in_memory()
    agent = _agent(tmp_path, call("push", {}, call_id="c1"), text("ok"))
    app = _app(agent, store=store)
    registry = app.state.deps.approvals
    orig_add = store.add_grants
    writing = asyncio.Event()
    release = asyncio.Event()

    async def slow_add(*args, **kwargs):
        writing.set()
        await release.wait()
        await orig_add(*args, **kwargs)

    store.add_grants = slow_add  # type: ignore[method-assign]
    async with _client(app) as ac:
        task, lines = await _run_until_approval(ac, "s1")
        approve = asyncio.create_task(
            ac.post(
                "/api/chat/approve",
                json={
                    "session_id": "s1",
                    "call_id": "c1",
                    "decision": "approve",
                    "remember": True,
                },
            )
        )
        await asyncio.wait_for(writing.wait(), timeout=5)
        assert registry.claim("s1", "c1") is False  # owned by the approve request
        await ac.post("/api/chat/cancel", params={"session_id": "s1"})
        release.set()
        r = await approve
        assert r.json() == {"ok": True, "remembered": True}
        await asyncio.wait_for(task, timeout=5)
        await _wait_run(ac, "s1", gone=True)
        assert [
            g["scope"] for g in (await ac.get("/api/sessions/s1/grants")).json()
        ] == ["shell:git push"]
    # The run was cancelled: the approved call did not go on to execute.
    assert not any(
        "pushed" in str(d.get("result")) for d in _frames(lines, "tool_result")
    )
