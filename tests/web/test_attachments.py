"""user_content: a message's attachments → multimodal content, vision-gated.

Images inline as ImagePart only when the model can see them; otherwise every
attachment is surfaced by path in a note. An upload is looked up by name in
the chat's attachment folder; a workspace file (the Files panel's "attach")
by its path, through a view of the workspace. Neither is taken on the
client's word — a traversal or a missing file is dropped.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path

import httpx
import pytest

from lovia import Agent, tool
from lovia.parts import ImagePart, TextPart
from lovia.web import ChatStore, create_app
from lovia.web.attachments import build_user_input
from lovia.web.schemas import Attachment, ChatRequest
from lovia.workspace import Workspace

from ..scripted_provider import ScriptedProvider, call, text

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _agent(root: Path, *, vision: bool) -> Agent:
    prov = ScriptedProvider([text("ok")])
    if vision:
        prov.supports_vision = True  # type: ignore[attr-defined]
    return Agent(name="bot", model=prov, workspace=Workspace.local(str(root)))


def _seed_image(root: Path, rel: str = "pics/cat.png") -> str:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(PNG)
    return rel


def _att(path: str, mime: str, kind: str) -> Attachment:
    """A workspace file, attached by path."""
    return Attachment(path=path, mime=mime, kind=kind, name=Path(path).name)


def _upload(name: str, mime: str = "image/png", kind: str = "image") -> Attachment:
    """An upload, named as its folder holds it."""
    return Attachment(name=name, mime=mime, kind=kind)


def _texts(parts: list) -> str:
    return " ".join(p.text for p in parts if isinstance(p, TextPart))


def _folder(tmp_path: Path, *files: tuple[str, bytes]) -> Path:
    folder = tmp_path / "store" / "attachments" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    for name, data in files:
        (folder / name).write_bytes(data)
    return folder


async def test_no_attachments_returns_plain_string(tmp_path: Path) -> None:
    agent = _agent(tmp_path, vision=True)
    assert await build_user_input(ChatRequest(message="hello"), agent, None) == "hello"


async def test_an_upload_is_inlined_and_named_by_absolute_path(tmp_path: Path) -> None:
    folder = _folder(tmp_path, ("cat.png", PNG))
    agent = _agent(tmp_path / "ws", vision=True)
    req = ChatRequest(message="what is this", attachments=[_upload("cat.png")])
    parts = (await build_user_input(req, agent, folder))[0].content
    images = [p for p in parts if isinstance(p, ImagePart)]
    assert len(images) == 1 and base64.b64decode(images[0].data) == PNG
    assert images[0].mime_type == "image/png"
    note = parts[-1].text
    # One path a line, the folder's own — never under the workspace.
    assert note == (
        f"\n\n[Attached:\n{(folder / 'cat.png').resolve()}\n"
        "Read with your file tools when relevant.]"
    )


async def test_the_note_lists_every_attachment_on_its_own_line(tmp_path: Path) -> None:
    # A data directory may hold ", " (``C:\Users\Li, Wei``): one path a line.
    folder = _folder(tmp_path / "Li, Wei", ("a.png", PNG), ("b c.csv", b"x,y\n"))
    _seed_image(tmp_path / "ws")
    agent = _agent(tmp_path / "ws", vision=False)
    req = ChatRequest(
        message="",
        attachments=[
            _upload("a.png"),
            _upload("b c.csv", "text/csv", "file"),
            _att("pics/cat.png", "image/png", "image"),
        ],
    )
    note = (await build_user_input(req, agent, folder))[0].content[-1].text
    assert note.splitlines()[1:4] == [
        str((folder / "a.png").resolve()),
        str((folder / "b c.csv").resolve()),
        "pics/cat.png",
    ]


@pytest.mark.parametrize("name", ["../x.png", "sub/cat.png", "..", "ghost.png"])
async def test_an_upload_outside_the_folder_or_missing_is_dropped(
    tmp_path: Path, name: str
) -> None:
    folder = _folder(tmp_path, ("cat.png", PNG))
    (folder.parent / "x.png").write_bytes(PNG)
    agent = _agent(tmp_path / "ws", vision=True)
    req = ChatRequest(message="hi", attachments=[_upload(name)])
    assert await build_user_input(req, agent, folder) == "hi"


async def test_an_upload_without_a_folder_is_dropped(tmp_path: Path) -> None:
    agent = _agent(tmp_path, vision=True)
    req = ChatRequest(message="hi", attachments=[_upload("cat.png")])
    assert await build_user_input(req, agent, None) == "hi"


async def test_vision_inlines_a_workspace_image_and_notes_its_path(
    tmp_path: Path,
) -> None:
    rel = _seed_image(tmp_path)
    agent = _agent(tmp_path, vision=True)
    req = ChatRequest(
        message="what is this", attachments=[_att(rel, "image/png", "image")]
    )
    out = await build_user_input(req, agent, None)
    assert isinstance(out, list) and len(out) == 1
    parts = out[0].content
    images = [p for p in parts if isinstance(p, ImagePart)]
    assert len(images) == 1  # inlined once
    assert base64.b64decode(images[0].data) == PNG
    assert "what is this" in _texts(parts)
    assert rel in _texts(parts)  # path still noted so tools can reach it


async def test_non_vision_chat_keeps_the_image_too(tmp_path: Path) -> None:
    # The chat keeps the image whatever its model: the runner puts a line of
    # text in its place for one that takes none, and a model the chat is
    # switched to later is shown it.
    folder = _folder(tmp_path, ("cat.png", PNG))
    agent = _agent(tmp_path / "ws", vision=False)
    req = ChatRequest(message="what is this", attachments=[_upload("cat.png")])
    parts = (await build_user_input(req, agent, folder))[0].content
    assert sum(isinstance(p, ImagePart) for p in parts) == 1
    assert str(folder.resolve() / "cat.png") in _texts(parts)


async def test_file_attachment_is_reference_only_even_with_vision(
    tmp_path: Path,
) -> None:
    folder = _folder(tmp_path, ("report.pdf", b"%PDF-1.4 test"))
    agent = _agent(tmp_path / "ws", vision=True)
    req = ChatRequest(
        message="summarize",
        attachments=[_upload("report.pdf", "application/pdf", "file")],
    )
    parts = (await build_user_input(req, agent, folder))[0].content
    assert not any(isinstance(p, ImagePart) for p in parts)
    assert str(folder.resolve() / "report.pdf") in _texts(parts)


async def test_path_traversal_is_dropped(tmp_path: Path) -> None:
    agent = _agent(tmp_path, vision=True)
    req = ChatRequest(
        message="", attachments=[_att("../../etc/passwd", "image/png", "image")]
    )
    # Nothing usable was attached → falls back to the (empty) text turn.
    assert await build_user_input(req, agent, None) == ""


async def test_what_the_agent_is_denied_cannot_be_attached(tmp_path: Path) -> None:
    # Workspace files are looked up with the Files panel's eyes: a path the
    # agent's policy hides, a directory, a link out of the root — none goes
    # into the message, by path or inline.
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    root = tmp_path / "root"
    (root / "pics").mkdir(parents=True)
    (root / "secret.png").write_bytes(PNG)
    (root / "escape.png").symlink_to(outside)
    prov = ScriptedProvider([text("ok")])
    prov.supports_vision = True  # type: ignore[attr-defined]
    agent = Agent(
        name="bot",
        model=prov,
        workspace=Workspace.local(str(root), denied_paths=("secret.png",)),
    )
    req = ChatRequest(
        message="see",
        attachments=[
            _att(p, "image/png", "image")
            for p in ("secret.png", "pics", "escape.png", str(outside))
        ],
    )
    assert await build_user_input(req, agent, None) == "see"


async def test_an_image_too_large_to_inline_is_still_attached_by_path(
    tmp_path: Path,
) -> None:
    from lovia.workspace import WorkspaceLimits

    folder = _folder(tmp_path, ("cat.png", PNG))
    prov = ScriptedProvider([text("ok")])
    prov.supports_vision = True  # type: ignore[attr-defined]
    workspace = Workspace.local(
        str(tmp_path / "ws"), limits=WorkspaceLimits(max_file_read_bytes=8)
    )
    agent = Agent(name="bot", model=prov, workspace=workspace)
    req = ChatRequest(message="see", attachments=[_upload("cat.png")])
    parts = (await build_user_input(req, agent, folder))[0].content
    assert not any(isinstance(p, ImagePart) for p in parts)
    assert "cat.png" in _texts(parts)


async def test_missing_file_is_dropped_but_message_survives(tmp_path: Path) -> None:
    agent = _agent(tmp_path, vision=True)
    req = ChatRequest(
        message="hi", attachments=[_att("pics/ghost.png", "image/png", "image")]
    )
    assert await build_user_input(req, agent, None) == "hi"


async def test_without_a_workspace_images_go_inline_and_nothing_else(
    tmp_path: Path,
) -> None:
    # No file tools: an image its model sees, inline; a file it would need
    # tools for, dropped; the note names the image without the tools line.
    folder = _folder(tmp_path, ("x.png", PNG), ("notes.txt", b"n"))
    prov = ScriptedProvider([text("ok")])
    prov.supports_vision = True  # type: ignore[attr-defined]
    agent = Agent(name="plain", model=prov)
    req = ChatRequest(
        message="hi",
        attachments=[_upload("x.png"), _upload("notes.txt", "text/plain", "file")],
    )
    parts = (await build_user_input(req, agent, folder))[0].content
    images = [p for p in parts if isinstance(p, ImagePart)]
    assert [base64.b64decode(p.data) for p in images] == [PNG]
    assert parts[-1].text == f"\n\n[Attached:\n{(folder / 'x.png').resolve()}]"
    # A workspace file (by path) has nowhere to be read from.
    linked = ChatRequest(
        message="hi", attachments=[_att("a.png", "image/png", "image")]
    )
    assert await build_user_input(linked, agent, folder) == "hi"


def test_an_attachment_names_a_file_or_gives_a_path() -> None:
    with pytest.raises(ValueError):
        Attachment(mime="image/png", kind="image")


def test_history_serialization_preserves_image_parts() -> None:
    """A multimodal turn must reach the client as parts, not a flattened string,
    or the UI can't render image thumbnails on reload."""
    from lovia.messages import user
    from lovia.web.api.serialization import _content

    out = _content(
        user([TextPart("hi"), ImagePart(data="AAAA", mime_type="image/png")])
    )
    assert isinstance(out, list)
    assert any(p["type"] == "image" and p["mime_type"] == "image/png" for p in out)
    assert any(p["type"] == "text" and p["text"] == "hi" for p in out)
    # Plain-text turns stay strings (the common case).
    assert _content(user("hello")) == "hello"


@pytest.mark.parametrize("route", ["inject", "stream"])
async def test_steer_with_attachments_reaches_the_run_as_parts(
    tmp_path: Path, route: str
) -> None:
    """A steer's attachments join the live run like a turn's (#326): the image
    inlined for a vision model, the path note after the text; the
    ``user_injected`` frame carries the text and note, no ``[image]`` marker."""
    release = asyncio.Event()

    @tool
    async def block() -> str:
        """Block until the test releases the run."""
        await release.wait()
        return "unblocked"

    prov = ScriptedProvider([call("block", {}, call_id="c1"), text("after")])
    prov.supports_vision = True  # type: ignore[attr-defined]
    agent = Agent(
        name="bot", model=prov, tools=[block], workspace=Workspace.local(str(tmp_path))
    )
    store = ChatStore.sqlite(tmp_path / "data" / "chat.db")
    app = create_app(agent, titles=False, store=store)
    sid = uuid.uuid4().hex
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        up = await ac.post(
            f"/api/sessions/{sid}/attachments",
            files={"file": ("cat.png", PNG, "image/png")},
        )
        assert up.status_code == 200, up.text
        steer = {
            "session_id": sid,
            "message": "look",
            "attachments": [{"name": "cat.png", "mime": "image/png", "kind": "image"}],
        }
        body: list[str] = []

        async def consume(payload: dict) -> None:
            async with ac.stream("POST", "/api/chat/stream", json=payload) as res:
                async for line in res.aiter_lines():
                    body.append(line)

        first = asyncio.create_task(consume({"message": "go", "session_id": sid}))
        for _ in range(250):  # turn 1 underway: the steer drains at turn 2
            if prov.calls:
                break
            await asyncio.sleep(0.02)
        if route == "inject":
            r = await ac.post("/api/chat/inject", json=steer)
            assert r.json()["accepted"]
            second = None
        else:
            second = asyncio.create_task(consume(steer))
            for _ in range(250):  # its first frame: it has injected and attached
                if body.count("event: session") == 2:
                    break
                await asyncio.sleep(0.02)
        release.set()
        await asyncio.wait_for(first, timeout=5)
        if second is not None:
            await asyncio.wait_for(second, timeout=5)

    steered = prov.calls[1][-1]  # drained at turn 2's start, after the tool result
    assert steered.role == "user" and isinstance(steered.content, list)
    assert any(isinstance(p, ImagePart) for p in steered.content)
    assert _texts(steered.content).startswith("look")
    path = (tmp_path / "data" / "attachments" / sid / "cat.png").resolve()
    assert str(path) in _texts(steered.content)
    frame = body.index("event: user_injected")
    content = json.loads(body[frame + 1].removeprefix("data:"))["content"]
    assert content.startswith(f"look\n\n[Attached:\n{path}\n")
    assert "[image]" not in content
