"""The describe_image tool (vision-as-a-tool) and its config gating.

The tool lets a text-only main model delegate "look at this image" to a vision
model, reading through the run's workspace session (same path pipeline as
read_file/view_image). Gating: registered when the config assigns a vision
role (not set to take no images) and a workspace exists; a model call offers
it only while the chat model can't see images — one that sees gets the core
``view_image`` instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.exceptions import ToolError
from lovia.run_context import RunContext
from lovia.web.builder import resolve_vision_tool
from lovia.web.config import ModelProfile
from lovia.web.vision import make_describe_image_tool
from lovia.workspace import (
    LocalWorkspaceSession,
    PermissionDeniedError,
    Workspace,
    WorkspacePolicy,
)

from ..scripted_provider import ScriptedProvider, text

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _ctx(session: LocalWorkspaceSession | None = None) -> RunContext:
    return RunContext(
        deps=None,
        entries=[],
        agent=None,  # type: ignore[arg-type]
        workspace=session,  # type: ignore[arg-type]
    )


def _session(tmp_path: Path, **kwargs) -> LocalWorkspaceSession:
    return LocalWorkspaceSession(root=str(tmp_path), **kwargs)


@pytest.mark.asyncio
async def test_describe_image_runs_vision_model_on_workspace_image(
    tmp_path: Path,
) -> None:
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "cat.png").write_bytes(PNG)
    prov = ScriptedProvider([text("a grey cat")])
    tool = make_describe_image_tool(prov)
    assert tool.name == "describe_image"

    out = await tool.invoke(
        {"path": "uploads/cat.png", "question": "what animal?"},
        _ctx(_session(tmp_path)),
    )
    assert out == "a grey cat"
    # The vision model actually received an image content part.
    sent = prov.calls[-1][-1].content
    assert any(getattr(p, "type", None) == "image" for p in sent)


@pytest.mark.asyncio
async def test_describe_image_denied_outside_workspace_by_policy(
    tmp_path: Path,
) -> None:
    tool = make_describe_image_tool(ScriptedProvider([text("x")]))
    session = _session(tmp_path, policy=WorkspacePolicy.readonly())
    with pytest.raises(PermissionDeniedError):
        await tool.invoke({"path": "/etc/passwd.png"}, _ctx(session))


@pytest.mark.asyncio
async def test_describe_image_reports_missing_and_unsupported(
    tmp_path: Path,
) -> None:
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "notes.txt").write_text("hi")
    tool = make_describe_image_tool(ScriptedProvider([text("x")]))
    session = _session(tmp_path)
    with pytest.raises(ToolError, match="Not a file"):
        await tool.invoke({"path": "uploads/ghost.png"}, _ctx(session))
    with pytest.raises(ToolError, match="Not a supported image type"):
        await tool.invoke({"path": "uploads/notes.txt"}, _ctx(session))


@pytest.mark.asyncio
async def test_describe_image_size_cap_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lovia.web.vision as vision

    (tmp_path / "big.png").write_bytes(b"x" * 64)
    monkeypatch.setattr(vision, "VIEW_IMAGE_MAX_BYTES", 10)
    tool = make_describe_image_tool(ScriptedProvider([text("x")]))
    with pytest.raises(ToolError, match="sips -Z 1568"):
        await tool.invoke({"path": "big.png"}, _ctx(_session(tmp_path)))


def test_describe_image_outside_read_asks_when_the_policy_does(
    tmp_path: Path,
) -> None:
    tool = make_describe_image_tool(ScriptedProvider([text("x")]))
    session = _session(tmp_path, policy=WorkspacePolicy(read_outside="ask"))
    ctx = _ctx(session)
    # Same read pipeline as read_file: outside-root reads ask, inside don't.
    assert tool.decide({"path": "/tmp/shot.png"}, ctx).action == "ask"
    assert tool.decide({"path": "shot.png"}, ctx).action == "allow"


def test_resolve_vision_tool_gating(tmp_path: Path) -> None:
    ws = Workspace.local(str(tmp_path))
    profile = ModelProfile(id="eyes", model="openai:qwen-vl", api_key="sk-vision")

    assert resolve_vision_tool(ws, None) is None  # no vision role
    assert resolve_vision_tool(None, profile) is None  # no workspace
    # Built whatever the chat model: a call offers it only while that model
    # can't see images (describes_images).
    tool = resolve_vision_tool(ws, profile)
    assert tool is not None and tool.name == "describe_image"
    assert tool.describes_images and not tool.returns_images
    # A vision role whose model is set to take no images can't describe one.
    blind = ModelProfile(id="eyes", model="openai:qwen", api_key="k", vision="off")
    assert resolve_vision_tool(ws, blind) is None

    # A vision model on its own endpoint: the overrides thread through cleanly.
    remote = ModelProfile(
        id="eyes2",
        model="openai:qwen-vl",
        base_url="https://dashscope.example/v1",
        api_key="sk-vision",
    )
    tool2 = resolve_vision_tool(ws, remote)
    assert tool2 is not None and tool2.name == "describe_image"


@pytest.mark.asyncio
async def test_describe_image_says_so_when_the_vision_model_refuses_it(
    tmp_path: Path,
) -> None:
    from lovia.exceptions import ProviderError

    class Refuses:
        name = "refuses"
        model = "m"
        supports_vision = True
        calls = 0

        async def stream(self, entries, **kwargs):
            Refuses.calls += 1
            body = "image exceeds 8000 pixels"
            raise ProviderError(body, status_code=400, body=body)
            yield  # pragma: no cover

    (tmp_path / "cat.png").write_bytes(PNG)
    tool = make_describe_image_tool(Refuses())  # type: ignore[arg-type]
    with pytest.raises(ToolError, match="refused the image"):
        await tool.invoke({"path": "cat.png"}, _ctx(_session(tmp_path)))
    # Never a description of the image's text marker.
    assert Refuses.calls == 1

    # A provider that declares it takes no images is never asked at all.
    blind = ScriptedProvider([text("a caption of a marker")])
    blind.supports_vision = False  # type: ignore[attr-defined]
    tool = make_describe_image_tool(blind)
    with pytest.raises(ToolError, match="refused the image"):
        await tool.invoke({"path": "cat.png"}, _ctx(_session(tmp_path)))
    assert blind.calls == []


def test_resolve_vision_tool_bad_profile_degrades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unusable vision profile disables the tool instead of crashing boot."""
    ws = Workspace.local(str(tmp_path))
    bad = ModelProfile(id="eyes", model="no-such-vendor:qwen-vl")
    with caplog.at_level("WARNING", logger="lovia.web.builder"):
        assert resolve_vision_tool(ws, bad) is None
    assert "describe_image disabled" in caplog.text
