"""The describe_image tool (vision-as-a-tool) and its config gating.

The tool lets a text-only main model delegate "look at this image" to a vision
model, reading through the run's workspace session (same path pipeline as
read_file/view_image). Gating: registered only when the config assigns a
vision role, a workspace exists, and the main model can't already see images —
a vision-capable main gets the core ``view_image`` instead.
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
        context=None,
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


def test_describe_image_outside_read_asks_under_coding_policy(
    tmp_path: Path,
) -> None:
    tool = make_describe_image_tool(ScriptedProvider([text("x")]))
    session = _session(tmp_path, policy=WorkspacePolicy.coding())
    ctx = _ctx(session)
    # Same read pipeline as read_file: outside-root reads ask, inside don't.
    assert tool.requires_approval({"path": "/tmp/shot.png"}, ctx) is True
    assert tool.requires_approval({"path": "shot.png"}, ctx) is False


def test_resolve_vision_tool_gating(tmp_path: Path) -> None:
    ws = Workspace.local(str(tmp_path))
    text_prov = ScriptedProvider([text("x")])  # no vision
    vision_prov = ScriptedProvider([text("x")])
    vision_prov.supports_vision = True  # type: ignore[attr-defined]
    profile = ModelProfile(id="eyes", model="openai:qwen-vl", api_key="sk-vision")

    assert resolve_vision_tool(text_prov, ws, None) is None  # no vision role
    assert resolve_vision_tool(text_prov, None, profile) is None  # no workspace
    # A vision-capable main model gets view_image instead — no delegation tool.
    assert resolve_vision_tool(vision_prov, ws, profile) is None
    tool = resolve_vision_tool(text_prov, ws, profile)
    assert tool is not None and tool.name == "describe_image"

    # A vision model on its own endpoint: the overrides thread through cleanly.
    remote = ModelProfile(
        id="eyes2",
        model="openai:qwen-vl",
        base_url="https://dashscope.example/v1",
        api_key="sk-vision",
    )
    tool2 = resolve_vision_tool(text_prov, ws, remote)
    assert tool2 is not None and tool2.name == "describe_image"


def test_resolve_vision_tool_bad_profile_degrades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unusable vision profile disables the tool instead of crashing boot."""
    ws = Workspace.local(str(tmp_path))
    text_prov = ScriptedProvider([text("x")])
    bad = ModelProfile(id="eyes", model="no-such-vendor:qwen-vl")
    with caplog.at_level("WARNING", logger="lovia.web.builder"):
        assert resolve_vision_tool(text_prov, ws, bad) is None
    assert "describe_image disabled" in caplog.text


def test_env_bool_parses_values_and_guards_model_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lovia.web.builder import _env_bool

    monkeypatch.delenv("X_FLAG", raising=False)
    assert _env_bool("X_FLAG") is None  # unset
    monkeypatch.setenv("X_FLAG", "1")
    assert _env_bool("X_FLAG") is True
    monkeypatch.setenv("X_FLAG", "off")
    assert _env_bool("X_FLAG") is False
    # The footgun (a model spec in a boolean flag) reads false, not truthy.
    monkeypatch.setenv("X_FLAG", "openai:qwen-vl")
    assert _env_bool("X_FLAG") is False
