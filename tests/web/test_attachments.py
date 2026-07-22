"""build_user_input: composer attachments → multimodal content, vision-gated.

Images inline as ImagePart only when the model can see them; otherwise every
attachment is surfaced as a workspace path in a note. Client paths are never
trusted — a traversal or a missing file is dropped.
"""

from __future__ import annotations

from pathlib import Path

from lovia import Agent
from lovia.parts import ImagePart, TextPart
from lovia.web.attachments import build_user_input
from lovia.web.schemas import Attachment, ChatRequest
from lovia.workspace import Workspace

from ..scripted_provider import ScriptedProvider, text

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _agent(root: Path, *, vision: bool) -> Agent:
    prov = ScriptedProvider([text("ok")])
    if vision:
        prov.supports_vision = True  # type: ignore[attr-defined]
    return Agent(name="bot", model=prov, workspace=Workspace.local(str(root)))


def _seed_image(root: Path, rel: str = "uploads/cat.png") -> str:
    (root / "uploads").mkdir(exist_ok=True)
    (root / rel).write_bytes(PNG)
    return rel


def _att(path: str, mime: str, kind: str) -> Attachment:
    return Attachment(path=path, mime=mime, kind=kind, name=Path(path).name)


def _texts(parts: list) -> str:
    return " ".join(p.text for p in parts if isinstance(p, TextPart))


def test_no_attachments_returns_plain_string(tmp_path: Path) -> None:
    agent = _agent(tmp_path, vision=True)
    assert build_user_input(ChatRequest(message="hello"), agent) == "hello"


def test_vision_inlines_image_and_notes_its_path(tmp_path: Path) -> None:
    rel = _seed_image(tmp_path)
    agent = _agent(tmp_path, vision=True)
    req = ChatRequest(message="what is this", attachments=[_att(rel, "image/png", "image")])
    out = build_user_input(req, agent)
    assert isinstance(out, list) and len(out) == 1
    parts = out[0].content
    assert sum(isinstance(p, ImagePart) for p in parts) == 1  # inlined once
    assert "what is this" in _texts(parts)
    assert rel in _texts(parts)  # path still noted so tools can reach it


def test_non_vision_references_path_without_inlining(tmp_path: Path) -> None:
    rel = _seed_image(tmp_path)
    agent = _agent(tmp_path, vision=False)
    req = ChatRequest(message="what is this", attachments=[_att(rel, "image/png", "image")])
    out = build_user_input(req, agent)
    parts = out[0].content
    assert not any(isinstance(p, ImagePart) for p in parts)  # NOT inlined
    assert rel in _texts(parts)


def test_file_attachment_is_reference_only_even_with_vision(tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "report.pdf").write_bytes(b"%PDF-1.4 test")
    agent = _agent(tmp_path, vision=True)
    req = ChatRequest(
        message="summarize",
        attachments=[_att("uploads/report.pdf", "application/pdf", "file")],
    )
    parts = build_user_input(req, agent)[0].content
    assert not any(isinstance(p, ImagePart) for p in parts)
    assert "uploads/report.pdf" in _texts(parts)


def test_path_traversal_is_dropped(tmp_path: Path) -> None:
    agent = _agent(tmp_path, vision=True)
    req = ChatRequest(
        message="", attachments=[_att("../../etc/passwd", "image/png", "image")]
    )
    # Nothing usable was attached → falls back to the (empty) text turn.
    assert build_user_input(req, agent) == ""


def test_missing_file_is_dropped_but_message_survives(tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    agent = _agent(tmp_path, vision=True)
    req = ChatRequest(
        message="hi", attachments=[_att("uploads/ghost.png", "image/png", "image")]
    )
    assert build_user_input(req, agent) == "hi"


def test_no_workspace_agent_ignores_attachments(tmp_path: Path) -> None:
    agent = Agent(name="plain", model=ScriptedProvider([text("ok")]))
    req = ChatRequest(message="hi", attachments=[_att("uploads/x.png", "image/png", "image")])
    assert build_user_input(req, agent) == "hi"


def test_history_serialization_preserves_image_parts() -> None:
    """A multimodal turn must reach the client as parts, not a flattened string,
    or the UI can't render image thumbnails on reload."""
    from lovia.messages import user
    from lovia.web.api.serialization import _content

    out = _content(user([TextPart("hi"), ImagePart(data="AAAA", mime_type="image/png")]))
    assert isinstance(out, list)
    assert any(p["type"] == "image" and p["mime_type"] == "image/png" for p in out)
    assert any(p["type"] == "text" and p["text"] == "hi" for p in out)
    # Plain-text turns stay strings (the common case).
    assert _content(user("hello")) == "hello"
