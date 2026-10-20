"""Message refs: each user turn's stable ``"<run_id>:<n>"`` id — in the session
view, the snapshots, the live stream's ``refs`` frame, and across a rewind."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from lovia import Agent  # noqa: E402
from lovia.checkpointer import RunHead  # noqa: E402
from lovia.messages import Usage  # noqa: E402
from lovia.transcript import AssistantTextEntry, InputEntry  # noqa: E402
from lovia.web.api.serialization import stamp_refs, user_turn_refs  # noqa: E402
from lovia.web.schemas import MessageOut  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402
from .test_supervisor import _blocking_tool, _client, _spawn, _wait_run  # noqa: E402
from .test_web import _app, _parse_sse  # noqa: E402


def _bot(script, **kw) -> Agent:
    return Agent(name="bot", model=ScriptedProvider(script), **kw)


def _user_refs(messages: list[dict]) -> list[str | None]:
    return [m["ref"] for m in messages if m["role"] == "user"]


async def _detail_refs(ac, sid: str) -> list[str | None]:
    return _user_refs((await ac.get(f"/api/sessions/{sid}")).json()["entries"])


async def _stream(ac, body: dict) -> list[tuple[str, dict]]:
    return _parse_sse((await ac.post("/api/chat/stream", json=body)).text)


def _frame(evs, name: str) -> dict:
    return next(d for e, d in evs if e == name)


async def test_a_run_names_its_turn_just_before_done() -> None:
    app = _app(_bot([text("a1"), text("a2")]))
    async with _client(app) as ac:
        first = await _stream(ac, {"message": "one"})
        kinds = [e for e, _ in first]
        assert kinds.index("refs") == kinds.index("done") - 1
        sid = _frame(first, "session")["session_id"]
        second = await _stream(ac, {"message": "two", "session_id": sid})
        refs = _frame(first, "refs")["refs"] + _frame(second, "refs")["refs"]
        # The live frames name exactly what a reload will show.
        assert await _detail_refs(ac, sid) == refs
    assert len(refs) == 2 and all(r.endswith(":0") for r in refs)
    assert refs[0].split(":")[0] != refs[1].split(":")[0]


async def test_a_reattach_names_finished_turns_and_the_live_one_when_done() -> None:
    release = asyncio.Event()
    agent = _bot(
        [text("a1"), call("block", {}), text("a2")], tools=[_blocking_tool(release)]
    )
    app = _app(agent)
    async with _client(app) as ac:
        await _stream(ac, {"message": "one", "session_id": "s1"})
        task, _ = _spawn(
            ac, "/api/chat/stream", json={"message": "two", "session_id": "s1"}
        )
        await _wait_run(ac, "s1")
        t2, lines = _spawn(ac, "/api/chat/reconnect", params={"session_id": "s1"})
        await asyncio.sleep(0.1)
        release.set()
        await asyncio.wait_for(asyncio.gather(task, t2), timeout=5)
        final = await _detail_refs(ac, "s1")
    evs = _parse_sse("\n".join(lines))
    # The live run's turn isn't named until the run has written it: its ref
    # comes with the refs frame, as for the tab that sent it.
    assert _user_refs(_frame(evs, "snapshot")["entries"]) == [final[0], None]
    assert _frame(evs, "refs") == {"refs": [final[1]]}
    assert None not in final


async def test_a_resume_snapshot_and_frame_name_the_checkpointed_turn() -> None:
    store = ChatStore.in_memory()
    sid = "s1"
    await store.session.append(
        sid,
        [InputEntry(role="user", content="q1"), AssistantTextEntry(content="a1")],
        run_id="run-0",
    )
    await store.upsert(sid, agent="bot")
    await store.checkpointer.append(
        "run-1",
        [InputEntry(role="user", content="q2")],
        RunHead(agent_name="bot", usage=Usage(), turns=1, status="interrupted"),
    )
    await store.set_active_run_id(sid, "run-1")
    app = _app(_bot([text("resumed")]), store=store)
    async with _client(app) as ac:
        # Before the reconnect, the interrupted run's turn has no name yet.
        assert await _detail_refs(ac, sid) == ["run-0:0", None]
        res = await ac.post("/api/chat/reconnect", params={"session_id": sid})
        evs = _parse_sse(res.text)
        assert await _detail_refs(ac, sid) == ["run-0:0", "run-1:0"]
    assert _user_refs(_frame(evs, "snapshot")["entries"]) == ["run-0:0", None]
    assert _frame(evs, "refs") == {"refs": ["run-1:0"]}


async def test_refs_survive_a_rewind_and_a_cut_one_never_returns() -> None:
    store = ChatStore.in_memory()
    sid = "s1"
    # One run holding two user turns (the second was queued mid-run).
    await store.session.append(
        sid,
        [
            InputEntry(role="user", content="q1"),
            AssistantTextEntry(content="a1"),
            InputEntry(role="user", content="q2"),
            AssistantTextEntry(content="a2"),
        ],
        run_id="run-0",
    )
    await store.upsert(sid, agent="bot")
    app = _app(_bot([text("a3")]), store=store)
    async with _client(app) as ac:
        assert await _detail_refs(ac, sid) == ["run-0:0", "run-0:1"]
        res = await ac.post(f"/api/sessions/{sid}/rewind", json={"user_turn": 1})
        assert _user_refs(res.json()["entries"]) == ["run-0:0"]
        # The retry takes the cut turn's place, not its ref.
        await _stream(ac, {"message": "q2 again", "session_id": sid})
        refs = await _detail_refs(ac, sid)
    assert refs[0] == "run-0:0"
    assert refs[1] != "run-0:1" and refs[1].endswith(":0")


async def test_rewind_by_ref_and_a_stale_ref_is_refused() -> None:
    app = _app(_bot([text("a1"), text("a2"), text("a3")]))
    async with _client(app) as ac:
        sid = _frame(await _stream(ac, {"message": "one"}), "session")["session_id"]
        await _stream(ac, {"message": "two", "session_id": sid})
        first, second = await _detail_refs(ac, sid)
        url = f"/api/sessions/{sid}/rewind"
        for body in ({}, {"ref": second, "user_turn": 1}):
            assert (await ac.post(url, json=body)).status_code == 422
        res = await ac.post(url, json={"ref": second})
        assert res.status_code == 200 and res.json()["removed"] == 2
        # Another tab, still showing `second`, can't cut whatever took its place.
        await _stream(ac, {"message": "two v2", "session_id": sid})
        stale = await ac.post(url, json={"ref": second})
        assert stale.status_code == 404
        assert stale.json()["detail"]["code"] == "turn_not_found"
        assert (await _detail_refs(ac, sid))[0] == first


async def test_a_reconnect_as_the_run_winds_down_still_gets_its_refs(
    monkeypatch,
) -> None:
    """The run has answered but is still clearing up when a tab reconnects:
    its snapshot leaves the run's turn unnamed, so the replay carries the
    refs frame along with `done`."""
    app = _app(_bot([text("a1")]))
    store = app.state.deps.store
    gate, held = asyncio.Event(), []
    clear = store.clear_active_run_id

    async def held_clear(*args, **kwargs):  # the run's cleanup, after `done`
        held.append(True)
        await gate.wait()
        return await clear(*args, **kwargs)

    monkeypatch.setattr(store, "clear_active_run_id", held_clear)
    async with _client(app) as ac:
        task, sent = _spawn(
            ac, "/api/chat/stream", json={"message": "one", "session_id": "s1"}
        )
        await _until(lambda: bool(held))
        late, lines = _spawn(ac, "/api/chat/reconnect", params={"session_id": "s1"})
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.wait_for(asyncio.gather(task, late), 5)
    evs = _parse_sse("\n".join(lines))
    kinds = [e for e, _ in evs]
    assert kinds.index("refs") < kinds.index("done")
    assert _frame(evs, "refs") == _frame(_parse_sse("\n".join(sent)), "refs")


async def _until(cond, timeout: float = 5.0) -> None:
    async def wait() -> None:
        while not cond():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout)


async def test_overlapping_rewinds_of_a_chat_take_turns(monkeypatch) -> None:
    """Each rewind maps its target over the transcript as it read it: one
    that overlaps another must read after it, or it cuts by a stale reading."""
    app = _app(_bot([text("a1"), text("a2")]))
    store = app.state.deps.store
    async with _client(app) as ac:
        sid = _frame(await _stream(ac, {"message": "one"}), "session")["session_id"]
        await _stream(ac, {"message": "two", "session_id": sid})
        _, second = await _detail_refs(ac, sid)
        gate, held = asyncio.Event(), []
        get = store.get_active_run_id

        async def held_get(session_id: str):  # the first rewind, past its read
            if not held:
                held.append(True)
                await gate.wait()
            return await get(session_id)

        monkeypatch.setattr(store, "get_active_run_id", held_get)
        url, body = f"/api/sessions/{sid}/rewind", {"ref": second}
        first = asyncio.create_task(ac.post(url, json=body))
        await _until(lambda: bool(held))
        later = asyncio.create_task(ac.post(url, json=body))
        await asyncio.sleep(0.05)
        gate.set()
        statuses = sorted([(await first).status_code, (await later).status_code])
    assert statuses == [200, 404]  # the later one finds `second` already cut


class _Held:
    """A provider whose calls wait for ``gate``: the test queues a message
    while the first one is in flight."""

    name = "held"
    model = None
    supports_json_schema = False

    def __init__(self, inner: ScriptedProvider) -> None:
        self.inner = inner
        self.gate = asyncio.Event()
        self.entered = 0

    async def stream(self, entries, **kwargs):
        self.entered += 1
        await self.gate.wait()
        async for delta in self.inner.stream(entries, **kwargs):
            yield delta


async def test_no_refs_frame_when_the_run_holds_turns_the_stream_never_showed() -> None:
    """An output-repair prompt is a user entry no event announces: the turns
    on screen (the sent message, then a queued one) aren't the run's, so the
    frame — stamped onto them by position — is held back."""
    from pydantic import BaseModel

    class Out(BaseModel):
        n: int

    provider = _Held(ScriptedProvider([text("not json"), text('{"n": 7}')]))
    app = _app(Agent(name="bot", model=provider, output_type=Out))
    async with _client(app) as ac:
        task, lines = _spawn(
            ac, "/api/chat/stream", json={"message": "go", "session_id": "s"}
        )
        await _until(lambda: provider.entered == 1)  # first model call
        res = await ac.post(
            "/api/chat/inject", json={"session_id": "s", "message": "queued"}
        )
        assert res.json()["accepted"]
        provider.gate.set()  # bad output → repair prompt → "queued" drained
        await asyncio.wait_for(task, 5)
        refs = await _detail_refs(ac, "s")
    kinds = [e for e, _ in _parse_sse("\n".join(lines))]
    assert "done" in kinds and "refs" not in kinds
    assert len(refs) == 3 and None not in refs  # go, the repair, queued: on load


def test_turns_are_numbered_within_their_run() -> None:
    def user(text: str) -> InputEntry:
        return InputEntry(role="user", content=text)

    runs = [
        ("r1", [user("a"), AssistantTextEntry(content="x"), user("b")]),
        (None, [user("c")]),  # in flight without a checkpoint
        ("r2", [InputEntry(role="system", content="s"), user("d")]),
    ]
    assert user_turn_refs(runs) == ["r1:0", "r1:1", None, "r2:0"]


def test_a_view_that_disagrees_with_its_runs_gets_no_refs() -> None:
    outs = [MessageOut(role="user", content="a"), MessageOut(role="user", content="b")]
    stamped = stamp_refs(outs, [("r1", [InputEntry(role="user", content="a")])])
    assert [o.ref for o in stamped] == [None, None]
