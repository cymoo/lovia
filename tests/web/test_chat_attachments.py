"""Chat-owned attachments: where they live, who shares them, when they go.

An upload lands in its chat's folder under the store's ``attachments_dir``,
never in a workspace. Forks and tasks share their source's folder (the
history names those files by absolute path); the folder goes with the last
chat that uses it. A run may read the folder and never write it, whatever
its mode. Design: docs/design/web-attachments.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, Header, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.transcript import InputEntry  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.attachments import safe_upload_name  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.web.workspaces import WorkspaceSessions, attachment_policy  # noqa: E402
from lovia.workspace import PathRule, Workspace, WorkspacePolicy  # noqa: E402

from ..scripted_provider import ScriptedProvider, call, text  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _sid() -> str:
    return uuid.uuid4().hex


async def _chat(store: ChatStore, sid: str, **kw: object) -> None:
    await store.upsert(sid, agent="bot", **kw)  # type: ignore[arg-type]
    await store.session.append(sid, [InputEntry(role="user", content="hi")])


def _put(folder: Path, name: str = "a.txt") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("x")
    return folder / name


# ------------------------------------------------------------------ store -


async def test_the_folder_sits_beside_the_database(tmp_path: Path) -> None:
    store = ChatStore.sqlite(tmp_path / "home" / "chat.db")
    sid = _sid()
    assert store.attachments_dir == tmp_path / "home" / "attachments"
    # A draft — no chat yet — already has its folder: the send creates the chat.
    assert await store.attachment_folder(sid) == store.attachments_dir / sid
    # An id that can't name a folder has none.
    assert await store.attachment_folder("../escape") is None
    assert await store.attachment_folder("s1") is None


async def test_a_custom_attachments_dir(tmp_path: Path) -> None:
    store = ChatStore(
        ChatStore.in_memory().session,
        meta_path=tmp_path / "meta.db",
        attachments_dir=tmp_path / "elsewhere",
    )
    assert store.attachments_dir == tmp_path / "elsewhere"


async def test_forks_and_tasks_share_the_folder_and_the_last_one_frees_it(
    tmp_path: Path,
) -> None:
    store = ChatStore.sqlite(tmp_path / "chat.db")
    src = _sid()
    await _chat(store, src)
    folder = await store.attachment_folder(src)
    assert folder is not None
    _put(folder)
    fork = await store.fork(src)
    assert fork is not None
    task = _sid()
    await store.upsert(task, agent="bot", parent_id=fork)
    # The fork's history names the source's files; its task reads them too.
    assert await store.attachment_folder(fork) == folder
    assert await store.attachment_folder(task) == folder
    await store.delete(src)
    assert folder.is_dir()  # the fork still uses it
    await store.delete(fork)  # and its task with it
    assert not folder.exists()


async def test_a_fork_of_a_fork_keeps_the_first_folder(tmp_path: Path) -> None:
    store = ChatStore.sqlite(tmp_path / "chat.db")
    src = _sid()
    await _chat(store, src)
    one = await store.fork(src)
    assert one is not None
    two = await store.fork(one)
    assert two is not None
    folder = await store.attachment_folder(src)
    assert await store.attachment_folder(two) == folder


async def test_a_chat_from_before_the_column_owns_its_own(tmp_path: Path) -> None:
    db = tmp_path / "chat.db"
    store = ChatStore.sqlite(db)
    sid = _sid()
    await _chat(store, sid)
    with sqlite3.connect(db) as conn:  # as a row from before 0.9.239 reads
        conn.execute("UPDATE chat_sessions SET attachments_id = NULL")
    folder = await store.attachment_folder(sid)
    assert folder == store.attachments_dir / sid
    _put(folder)
    await store.delete(sid)
    assert not folder.exists()


async def test_delete_all_leaves_what_is_not_an_attachment_folder(
    tmp_path: Path,
) -> None:
    store = ChatStore.sqlite(tmp_path / "chat.db")
    sid = _sid()
    await _chat(store, sid)
    folder = await store.attachment_folder(sid)
    assert folder is not None
    _put(folder)
    _put(store.attachments_dir / "README", "keep.txt")
    await store.delete_all()
    assert not folder.exists()
    assert (store.attachments_dir / "README" / "keep.txt").is_file()


async def test_the_sweep_takes_only_old_unclaimed_folders(tmp_path: Path) -> None:
    store = ChatStore.sqlite(tmp_path / "chat.db")
    claimed = _sid()
    await _chat(store, claimed)
    root = store.attachments_dir
    old, fresh, legacy = _sid(), _sid(), _sid()
    for name in (old, fresh):
        await store.make_folder(root / name)
    for name in (claimed, old, fresh, legacy, "notes"):
        _put(root / name)
    an_hour_ago = time.time() - 3600
    for name in (claimed, old, legacy, "notes"):
        os.utime(root / name, (an_hour_ago, an_hour_ago))
    await store.sweep_attachments(max_age=60)
    # A folder from before the owner mark (``legacy``) may be another store's.
    kept = sorted(p.name for p in root.iterdir())
    assert kept == sorted([claimed, fresh, legacy, "notes"])


async def test_stores_side_by_side_leave_each_other_be(tmp_path: Path) -> None:
    # ``create_app``'s default: ``.lovia/<agent>.db``, one root for both.
    a = ChatStore.sqlite(tmp_path / "a.db")
    b = ChatStore.sqlite(tmp_path / "b.db")
    assert a.attachments_dir == b.attachments_dir
    root = a.attachments_dir
    chat, draft, mine = _sid(), _sid(), _sid()
    await _chat(b, chat)
    for store, name in ((b, chat), (b, draft), (a, mine)):
        await store.make_folder(root / name)
        _put(root / name)
        an_hour_ago = time.time() - 3600
        os.utime(root / name, (an_hour_ago, an_hour_ago))
    await a.sweep_attachments(max_age=60)
    assert sorted(p.name for p in root.iterdir()) == sorted([chat, draft])
    await a.delete_all()
    assert sorted(p.name for p in root.iterdir()) == sorted([chat, draft])
    await b.sweep_attachments(max_age=60)  # its own draft goes
    assert [p.name for p in root.iterdir()] == [chat]
    await b.delete_all()
    assert list(root.iterdir()) == []


async def test_a_folder_a_delete_left_is_still_the_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows: a file held open survives the delete, its owner mark may not.
    import lovia.web.store as store_module

    store = ChatStore.sqlite(tmp_path / "chat.db")
    sid = _sid()
    await _chat(store, sid)
    folder = await store.attachment_folder(sid)
    assert folder is not None
    await store.make_folder(folder)
    _put(folder)

    def held_open(path, ignore_errors=False):
        (Path(path) / ".lovia-owner").unlink(missing_ok=True)

    monkeypatch.setattr(store_module.shutil, "rmtree", held_open)
    await store.delete(sid)
    monkeypatch.undo()
    assert (folder / ".lovia-owner").is_file()
    an_hour_ago = time.time() - 3600
    os.utime(folder, (an_hour_ago, an_hour_ago))
    await store.sweep_attachments(max_age=60)  # closed by now
    assert not folder.exists()


async def test_the_owner_mark_is_not_an_attachment(tmp_path: Path) -> None:
    from lovia.web.attachments import _stats

    store = ChatStore.sqlite(tmp_path / "chat.db")
    folder = store.attachments_dir / _sid()
    await store.make_folder(folder)
    _put(folder, "a.txt")
    assert list(_stats(folder)) == ["a.txt"]  # what a remote host is sent


async def test_the_sweep_spares_a_folder_claimed_after_its_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A draft idle past the age, sent while the sweep runs: its chat appears
    # after the claims were read — the sweep asks again before removing.
    store = ChatStore.sqlite(tmp_path / "chat.db")
    draft = _sid()
    folder = store.attachments_dir / draft
    await store.make_folder(folder)
    _put(folder)
    an_hour_ago = time.time() - 3600
    os.utime(folder, (an_hour_ago, an_hour_ago))
    real = store._read_all

    async def snapshot_then_send(*args, **kwargs):
        rows = await real(*args, **kwargs)
        await _chat(store, draft)  # the send lands right after the snapshot
        return rows

    monkeypatch.setattr(store, "_read_all", snapshot_then_send)
    await store.sweep_attachments(max_age=60)
    assert folder.is_dir()


async def test_a_send_freshens_its_folder_for_the_sweep(tmp_path: Path) -> None:
    from lovia.web.attachments import user_content
    from lovia.web.schemas import Attachment

    store = ChatStore.sqlite(tmp_path / "chat.db")
    folder = store.attachments_dir / _sid()
    await store.make_folder(folder)
    _put(folder, "a.txt")
    an_hour_ago = time.time() - 3600
    os.utime(folder, (an_hour_ago, an_hour_ago))
    (tmp_path / "ws").mkdir()
    agent = Agent(
        name="bot", model=None, workspace=Workspace.local(str(tmp_path / "ws"))
    )
    att = Attachment(name="a.txt", mime="text/plain", kind="file")
    await user_content("see", [att], agent, folder)
    await store.sweep_attachments(max_age=60)  # its chat isn't created yet
    assert (folder / "a.txt").is_file()


# ------------------------------------------------------------------- API -


def _client(tmp_path: Path, agent: Agent | None = None) -> TestClient:
    agent = agent or Agent(
        name="bot",
        model=ScriptedProvider([text("ok")] * 4),
        workspace=Workspace.local(str(tmp_path / "ws")),
    )
    (tmp_path / "ws").mkdir(exist_ok=True)
    store = ChatStore.sqlite(tmp_path / "data" / "chat.db")
    return TestClient(create_app(agent, store=store, titles=False))


def _upload(c: TestClient, sid: str, name: str, data: bytes = b"data"):
    return c.post(f"/api/sessions/{sid}/attachments", files={"file": (name, data)})


def test_an_upload_lands_in_the_chats_folder_not_the_workspace(
    tmp_path: Path,
) -> None:
    sid = _sid()
    with _client(tmp_path) as c:
        r = _upload(c, sid, "cat.png", PNG)
        assert r.status_code == 200, r.text
        assert r.json() == {
            "name": "cat.png",
            "mime": "image/png",
            "kind": "image",
            "size": len(PNG),
        }
        shown = c.get(f"/api/sessions/{sid}/attachments/cat.png")
        assert shown.status_code == 200 and shown.content == PNG
        assert shown.headers["content-type"] == "image/png"
        assert shown.headers["x-content-type-options"] == "nosniff"
    assert (tmp_path / "data" / "attachments" / sid / "cat.png").read_bytes() == PNG
    assert not (tmp_path / "ws" / "uploads").exists()


def test_the_draft_id_becomes_the_chat(tmp_path: Path) -> None:
    sid = _sid()
    with _client(tmp_path) as c:
        assert _upload(c, sid, "notes.txt").status_code == 200
        res = c.post(
            "/api/chat",
            json={
                "message": "read it",
                "session_id": sid,
                "attachments": [
                    {"name": "notes.txt", "mime": "text/plain", "kind": "file"}
                ],
            },
        )
        assert res.status_code == 200, res.text
        assert res.json()["session_id"] == sid
        model = c.app.state.deps.agents["bot"].model
        note = model.calls[-1][-1].content[-1].text
        assert str(tmp_path / "data" / "attachments" / sid / "notes.txt") in note


def test_a_fork_shows_its_sources_attachments_after_the_source_goes(
    tmp_path: Path,
) -> None:
    sid = _sid()
    with _client(tmp_path) as c:
        assert _upload(c, sid, "cat.png", PNG).status_code == 200
        c.post("/api/chat", json={"message": "hi", "session_id": sid})
        fork = c.post(f"/api/sessions/{sid}/fork", json={}).json()["session_id"]
        assert c.delete(f"/api/sessions/{sid}").is_success
        assert c.get(f"/api/sessions/{fork}/attachments/cat.png").content == PNG
        assert c.delete(f"/api/sessions/{fork}").is_success
        gone = c.get(f"/api/sessions/{fork}/attachments/cat.png")
        assert gone.status_code == 404
    assert not (tmp_path / "data" / "attachments" / sid).exists()


def test_an_agent_without_a_workspace_takes_images_only(tmp_path: Path) -> None:
    # A file it would need tools to read is refused up front, not lost on
    # send; an image goes in, and the page learns whether its model sees.
    prov = ScriptedProvider([text("ok")])
    prov.supports_vision = True  # type: ignore[attr-defined]
    plain = Agent(name="bot", model=prov)
    with _client(tmp_path, plain) as c:
        info = c.get("/api/agents").json()[0]
        assert {k: info[k] for k in ("workspace", "vision")} == {
            "workspace": False,
            "vision": True,
        }
        r = _upload(c, _sid(), "a.txt")
        assert (r.status_code, r.json()["detail"]["code"]) == (
            415,
            "unsupported_file_type",
        )
        assert _upload(c, _sid(), "cat.png", PNG).status_code == 200
        # Larger than inlined would be accepted and never seen: refused.
        from lovia.workspace import WorkspaceLimits

        big = PNG + b"\0" * WorkspaceLimits().max_file_read_bytes
        assert _upload(c, _sid(), "big.png", big).status_code == 413


def test_a_vendor_model_string_is_resolved_for_vision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "openai:gpt-5.5" says nothing itself: its provider decides, as in a run.
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    agent = Agent(name="bot", model="openai:gpt-5.5")
    with _client(tmp_path, agent) as c:
        assert c.get("/api/agents").json()[0]["vision"] is True


async def test_an_in_memory_stores_folder_goes_with_it() -> None:
    import gc

    store = ChatStore.in_memory()
    root = store.attachments_dir
    _put(root / _sid())
    del store
    gc.collect()
    assert not root.exists()


@pytest.mark.parametrize("sid", ["s1", "ABCDEF0123456789ABCDEF0123456789"])
def test_an_id_that_cannot_name_a_folder_is_refused(tmp_path: Path, sid: str) -> None:
    with _client(tmp_path) as c:
        r = _upload(c, sid, "a.txt")
        assert r.status_code == 422
        assert "hex" in r.json()["detail"]["hint"]


# (A bare ``..`` segment never arrives: the client normalizes it away.)
@pytest.mark.parametrize(
    "name", ["..%2Fchat.db", "%2E%2E", ".hidden", ".lovia-owner", "a%5Cb.txt"]
)
def test_only_a_plain_name_is_served(tmp_path: Path, name: str) -> None:
    sid = _sid()
    with _client(tmp_path) as c:
        assert _upload(c, sid, "a.txt").status_code == 200
        r = c.get(f"/api/sessions/{sid}/attachments/{name}")
        assert r.status_code == 404


def test_upload_sanitizes_and_dedupes_names(tmp_path: Path) -> None:
    sid = _sid()
    with _client(tmp_path) as c:
        assert _upload(c, sid, "../../evil.txt").json()["name"] == "evil.txt"
        assert _upload(c, sid, "evil.txt").json()["name"] == "evil-1.txt"
    folder = tmp_path / "data" / "attachments" / sid
    names = sorted(p.name for p in folder.iterdir())
    assert names == [".lovia-owner", "evil-1.txt", "evil.txt"]


@pytest.mark.parametrize(
    "raw",
    [
        "x" * 119 + ".txt",  # the cut lands on the extension's dot
        "x" * 118 + " .txt",  # ... or on a space
        "x" * 200 + ".pdf",
        "y" * 117 + ". .png",
        "." * 130,
        "con" + "z" * 200 + ".txt",
        "a." + "b" * 150,  # an "extension" longer than half the cap
        "报告" * 80 + ".xlsx",
    ],
)
def test_every_saved_name_reads_back_as_itself(raw: str) -> None:
    from lovia.web.attachments import _candidates, is_attachment_name

    name = safe_upload_name(raw)
    assert len(name) <= 120 and is_attachment_name(name), name
    candidates = _candidates(name)
    for candidate in [next(candidates) for _ in range(12)]:
        assert len(candidate) <= 120 and is_attachment_name(candidate), candidate


def test_a_long_name_keeps_its_extension() -> None:
    name = safe_upload_name("x" * 200 + ".pdf")
    assert name == "x" * 116 + ".pdf"


def test_a_caller_owned_session_takes_no_upload(tmp_path: Path) -> None:
    # One session the caller bound serves every chat of the agent: a chat's
    # folder can't be made read-only there for that chat alone.
    from lovia.workspace.workspace import WorkspaceSessionBinding

    cfg = Workspace.local(str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir()
    bound = WorkspaceSessionBinding(cfg, None)  # type: ignore[arg-type]
    agent = Agent(name="bot", model=ScriptedProvider([text("ok")]), workspace=bound)
    with _client(tmp_path, agent) as c:
        r = _upload(c, _sid(), "a.txt")
    detail = r.json()["detail"]
    assert (r.status_code, detail["code"]) == (404, "feature_unavailable")
    assert "bound session" in detail["hint"]


def test_names_stay_distinct_on_any_host_filesystem(tmp_path: Path) -> None:
    # A Linux folder holds A.txt and a.txt apart; a macOS or Windows host
    # copying it would not — so the second upload takes a suffix.
    from lovia.web.attachments import save_attachment

    folder = tmp_path / _sid()
    assert save_attachment(folder, "A.txt", b"1") == "A.txt"
    assert save_attachment(folder, "a.txt", b"2") == "a-1.txt"
    assert save_attachment(folder, "é.txt", b"3") == "é.txt"
    assert save_attachment(folder, "é.txt", b"4") == "é-1.txt"


def test_a_deduped_long_name_is_still_served_and_sent(tmp_path: Path) -> None:
    # The suffix must not push a 120-character name past the cap: such a
    # name would be saved, then refused by every read.
    sid = _sid()
    long = "x" * 116 + ".txt"
    with _client(tmp_path) as c:
        assert _upload(c, sid, long).json()["name"] == long
        names = [_upload(c, sid, long).json()["name"] for _ in range(11)]
        for name in names:
            assert len(name) <= 120 and name.endswith(".txt")
            assert c.get(
                f"/api/sessions/{sid}/attachments/{name}", params={"download": 1}
            ).is_success
        assert len(set(names)) == 11
        assert names[-1].endswith("-11.txt")


@pytest.mark.parametrize(
    ("raw", "saved"),
    [
        ("con.png", "_con.png"),
        ("NUL", "_NUL"),
        ("lpt1.tar.gz", "_lpt1.tar.gz"),
        ("console.png", "console.png"),
        ("com10.txt", "com10.txt"),
    ],
)
def test_windows_device_names_are_never_saved(raw: str, saved: str) -> None:
    assert safe_upload_name(raw) == saved
    assert safe_upload_name(saved) == saved  # idempotent: names round-trip


def test_upload_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _sid()
    with _client(tmp_path) as c:
        assert _upload(c, sid, "empty.txt", b"").status_code == 422
        assert _upload(c, sid, "payload.exe", b"MZ").status_code == 415
        assert _upload(c, sid, "Makefile", b"all:\n").json()["kind"] == "file"
        monkeypatch.setenv("LOVIA_UPLOAD_ALLOWED_EXT", "png, jpg")
        assert _upload(c, sid, "notes.txt").status_code == 415
        monkeypatch.setenv("LOVIA_UPLOAD_ALLOWED_EXT", "*")
        assert _upload(c, sid, "weird.xyz").status_code == 200
        monkeypatch.setenv("LOVIA_MAX_UPLOAD_MB", "1")
        assert _upload(c, sid, "big.txt", b"x" * (1024 * 1024 + 1)).status_code == 413


MB = 1024 * 1024


async def _stream_upload(
    app: FastAPI,
    size: int,
    *,
    part: str = 'name="file"; filename="a.txt"',
    headers: dict[str, str] | None = None,
) -> tuple[int, object, int]:
    """Upload one ``size``-byte part in 64 KiB chunks, no Content-Length.
    Returns the status, the JSON answer, and the body bytes the app took."""
    chunks = [f"--b\r\nContent-Disposition: form-data; {part}\r\n\r\n".encode()]
    chunks += [b"x" * 65536] * (size // 65536) + [b"x" * (size % 65536)]
    chunks.append(b"\r\n--b--\r\n")
    pending, taken, sent = iter(chunks), 0, []

    async def receive() -> dict[str, object]:
        nonlocal taken
        chunk = next(pending, None)
        taken += len(chunk or b"")
        return {
            "type": "http.request",
            "body": chunk or b"",
            "more_body": chunk is not None,
        }

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    path = f"/api/sessions/{_sid()}/attachments"
    all_headers = {"content-type": "multipart/form-data; boundary=b", **(headers or {})}
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in all_headers.items()],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }
    await app(scope, receive, send)  # type: ignore[arg-type]
    answer = b"".join(m.get("body", b"") for m in sent)
    return sent[0]["status"], json.loads(answer), taken


def _guarded(tmp_path: Path, **kw: object) -> FastAPI:
    (tmp_path / "ws").mkdir(exist_ok=True)
    agent = Agent(
        name="bot",
        model=ScriptedProvider([text("ok")]),
        workspace=Workspace.local(str(tmp_path / "ws")),
    )
    store = ChatStore.sqlite(tmp_path / "data" / "chat.db")
    return create_app(agent, store=store, titles=False, **kw)  # type: ignore[arg-type]


async def test_a_refused_upload_is_not_read(tmp_path: Path) -> None:
    app = _guarded(tmp_path, token="secret")
    status, answer, taken = await _stream_upload(app, 2 * MB)
    assert status == 401 and answer["detail"]["code"] == "server_token"  # type: ignore[index]
    assert taken == 0
    ok = {"authorization": "Bearer secret"}
    assert (await _stream_upload(app, 2 * MB, headers=ok))[0] == 200


async def _raises(request: Request) -> None:
    if request.headers.get("x-key") != "k":
        raise HTTPException(403, "no")


async def _validates(x_key: str = Header()) -> None:
    # A missing header is a validation error: FastAPI collects it and still
    # solves the other dependencies before it answers.
    pass


@pytest.mark.parametrize("auth, refusal", [(_raises, 403), (_validates, 422)])
async def test_an_upload_a_custom_auth_refuses_is_not_read(
    tmp_path: Path, auth: object, refusal: int
) -> None:
    app = _guarded(tmp_path, auth=auth)
    status, _, taken = await _stream_upload(app, 2 * MB)
    assert (status, taken) == (refusal, 0)
    assert (await _stream_upload(app, 2 * MB, headers={"x-key": "k"}))[0] == 200


async def _reads_form(request: Request) -> None:
    await request.form()


async def _reads_body(request: Request) -> None:
    await request.body()


@pytest.mark.parametrize("auth", [_reads_form, _reads_body])
async def test_an_auth_that_reads_the_body_leaves_it_to_the_upload(
    tmp_path: Path, auth: object
) -> None:
    app = _guarded(tmp_path, auth=auth)
    assert (await _stream_upload(app, 2 * MB))[0] == 200
    status, _, taken = await _stream_upload(app, 60 * MB)
    assert status == 413 and taken < 27 * MB


async def test_a_form_the_auth_read_and_refused_is_closed(tmp_path: Path) -> None:
    held: list[UploadFile] = []

    async def auth(request: Request) -> None:
        held.append((await request.form())["file"])  # type: ignore[arg-type]
        raise HTTPException(403, "no")

    app = _guarded(tmp_path, auth=auth)
    assert (await _stream_upload(app, 2 * MB))[0] == 403
    assert held[0].file.closed


@pytest.mark.parametrize(
    "part", ['name="file"; filename="a.txt"', 'name="other"; filename="a.txt"']
)
async def test_an_upload_over_the_cap_is_refused_as_it_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, part: str
) -> None:
    monkeypatch.setenv("LOVIA_MAX_UPLOAD_MB", "1")
    app = _guarded(tmp_path)
    status, answer, taken = await _stream_upload(app, 60 * MB, part=part)
    assert status == 413 and answer["detail"]["code"] == "file_too_large"  # type: ignore[index]
    assert taken < 3 * MB


async def test_an_upload_of_exactly_the_cap_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOVIA_MAX_UPLOAD_MB", "1")
    app = _guarded(tmp_path)
    status, answer, _ = await _stream_upload(app, MB)
    assert status == 200 and answer["size"] == MB  # type: ignore[index]
    assert (await _stream_upload(app, MB + 1))[0] == 413


def test_a_form_without_a_file_is_a_validation_error(tmp_path: Path) -> None:
    form = "multipart/form-data"
    with _client(tmp_path) as c:
        url = f"/api/sessions/{_sid()}/attachments"
        for kw in ({}, {"data": {"file": "x"}}, {"files": {"file": (None, "x")}}):
            r = c.post(url, **kw)  # type: ignore[arg-type]
            assert r.status_code == 422
            assert r.json()["detail"][0]["loc"] == ["body", "file"]
        for kind in (form, f"{form}; boundary=b"):
            r = c.post(url, content=b"junk", headers={"content-type": kind})
            assert r.status_code == 400
            assert isinstance(r.json()["detail"], str)
        # The schema still says what to send.
        spec = c.app.openapi()  # type: ignore[attr-defined]
    body = spec["paths"]["/api/sessions/{session_id}/attachments"]["post"][
        "requestBody"
    ]
    assert body["required"] is True and form in body["content"]


def test_svg_is_downloaded_never_shown(tmp_path: Path) -> None:
    sid = _sid()
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    with _client(tmp_path) as c:
        assert _upload(c, sid, "d.svg", svg).json()["kind"] == "file"
        assert c.get(f"/api/sessions/{sid}/attachments/d.svg").status_code == 415
        dl = c.get(f"/api/sessions/{sid}/attachments/d.svg", params={"download": 1})
        assert dl.status_code == 200
        assert dl.headers["content-disposition"].startswith("attachment")


def test_an_upload_the_disk_refuses_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    from lovia.web.api import attachments

    def full(folder, name, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(attachments, "save_attachment", full)
    with _client(tmp_path) as c:
        r = _upload(c, _sid(), "a.txt")
    detail = r.json()["detail"]
    assert (r.status_code, detail["code"]) == (500, "attachment_io_error")
    assert "No space left on device" in detail["message"]


# ---------------------------------------------------------------- policy -


@pytest.mark.parametrize("preset", ["readonly", "coding", "trusted"])
def test_a_run_reads_the_folder_and_never_writes_it(
    tmp_path: Path, preset: str
) -> None:
    folder = tmp_path / "attachments" / _sid()
    base = getattr(WorkspacePolicy, preset)()
    # Even a deployer's own rule that allows writing there doesn't undo it.
    widened = WorkspacePolicy.trusted(
        path_rules=(PathRule(folder.as_posix(), "allow"),)
    )
    for policy in (base, widened):
        guarded = attachment_policy(policy, folder.resolve().as_posix())
        target = (folder / "cat.png").resolve().as_posix()
        assert guarded.decide_path(rel=None, abs_posix=target, op="read").action == (
            "allow"
        )
        write = guarded.decide_path(rel=None, abs_posix=target, op="write")
        assert write.action == "deny"
        if policy.write_outside == "allow":  # only the folder's rule says no
            assert "read-only" in write.reason
        # Idempotent.
        assert attachment_policy(guarded, folder.resolve().as_posix()) == guarded


def test_glob_characters_in_the_data_directory_match_themselves(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "Work [2026] *" / "attachments" / _sid()
    folder.mkdir(parents=True)
    guarded = attachment_policy(WorkspacePolicy.trusted(), folder.resolve().as_posix())
    target = (folder / "cat.png").resolve().as_posix()
    assert guarded.decide_path(rel=None, abs_posix=target, op="write").action == "deny"
    # And they stay literal: a sibling the brackets would match as a class.
    other = (tmp_path / "Work 2" / "attachments" / folder.name / "x").as_posix()
    assert guarded.decide_path(rel=None, abs_posix=other, op="write").action == "allow"


def test_handoff_targets_read_the_folder_and_never_write_it(tmp_path: Path) -> None:
    from lovia.handoff import Handoff
    from lovia.web.workspaces import with_attachments

    folder = tmp_path / "attachments" / _sid()
    ws = Workspace.local(str(tmp_path / "ws"), mode="trusted")
    helper = Agent(name="helper", model=None, workspace=ws)
    writer = Agent(name="writer", model=None, workspace=ws, handoffs=[helper])
    helper.handoffs.append(Handoff(target=writer))  # a cycle
    guarded = with_attachments(writer, folder)
    target = (folder / "a.txt").resolve().as_posix()
    reached = guarded.handoffs[0]
    for agent in (guarded, reached, reached.handoffs[0].target):
        policy = agent.workspace.policy  # type: ignore[union-attr]
        assert policy.decide_path(rel=None, abs_posix=target, op="write").action == (
            "deny"
        )
    assert reached.handoffs[0].target is guarded  # the cycle resolves to the copy
    assert writer.workspace is ws  # the original is untouched


def test_denied_paths_still_win_over_the_read(tmp_path: Path) -> None:
    folder = tmp_path / "attachments" / _sid()
    policy = WorkspacePolicy.coding(denied_paths=(tmp_path.resolve().as_posix(),))
    target = (folder / "cat.png").resolve().as_posix()
    guarded = attachment_policy(policy, folder.resolve().as_posix())
    assert guarded.decide_path(rel=None, abs_posix=target, op="read").action == "deny"


async def test_a_mode_switch_keeps_the_folder_read_only(tmp_path: Path) -> None:
    folder = tmp_path / "attachments" / _sid()
    ws = tmp_path / "ws"
    ws.mkdir()
    sessions = WorkspaceSessions()
    agent = Agent(name="bot", model=None, workspace=Workspace.local(str(ws)))
    await sessions.bind("chat", agent, folder)
    session = sessions.get("chat")
    assert session is not None
    target = str(folder / "x.txt")
    try:
        assert session.decide_path(target, write=True).action == "deny"
        trusted = Workspace.local(str(ws), mode="trusted")
        assert sessions.retune("chat", trusted)
        assert session.decide_path(target).action == "allow"
        assert session.decide_path(target, write=True).action == "deny"
    finally:
        await sessions.aclose()


def test_a_trusted_run_cannot_overwrite_an_attachment(tmp_path: Path) -> None:
    sid = _sid()
    folder = tmp_path / "data" / "attachments" / sid
    target = folder / "notes.txt"
    prov = ScriptedProvider(
        [
            call("write_file", {"path": str(target), "content": "gone"}, call_id="w"),
            text("done"),
        ]
    )
    agent = Agent(
        name="bot",
        model=prov,
        workspace=Workspace.local(str(tmp_path / "ws"), mode="trusted"),
    )
    with _client(tmp_path, agent) as c:
        assert _upload(c, sid, "notes.txt", b"original").status_code == 200
        res = c.post("/api/chat", json={"message": "go", "session_id": sid})
        assert res.status_code == 200, res.text
    assert target.read_bytes() == b"original"
    result = str(prov.calls[1][-1].content)
    assert "read-only" in result
