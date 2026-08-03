"""The see_image tool (vision-as-a-tool) and its config gating.

The tool lets a text-only main model delegate "look at this image" to a vision
model, reading only workspace files. Gating: registered only when the config
assigns a vision role, a workspace exists, and the main model can't already
see images.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.run_context import RunContext
from lovia.web.builder import resolve_vision_tool
from lovia.web.config import ModelProfile
from lovia.web.vision import make_see_image_tool
from lovia.workspace import Workspace

from ..scripted_provider import ScriptedProvider, text

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _ctx() -> RunContext:
    return RunContext(context=None, entries=[], agent=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_see_image_runs_vision_model_on_workspace_image(tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "cat.png").write_bytes(PNG)
    prov = ScriptedProvider([text("a grey cat")])
    tool = make_see_image_tool(prov, workspace_root=tmp_path)
    assert tool.name == "see_image"

    out = await tool.invoke(
        {"path": "uploads/cat.png", "question": "what animal?"}, _ctx()
    )
    assert out == "a grey cat"
    # The vision model actually received an image content part.
    sent = prov.calls[-1][-1].content
    assert any(getattr(p, "type", None) == "image" for p in sent)


@pytest.mark.asyncio
async def test_see_image_refuses_paths_outside_workspace(tmp_path: Path) -> None:
    tool = make_see_image_tool(ScriptedProvider([text("x")]), workspace_root=tmp_path)
    out = await tool.invoke({"path": "../../etc/passwd"}, _ctx())
    assert "outside the workspace" in out


@pytest.mark.asyncio
async def test_see_image_reports_missing_and_unsupported(tmp_path: Path) -> None:
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "notes.txt").write_text("hi")
    tool = make_see_image_tool(ScriptedProvider([text("x")]), workspace_root=tmp_path)
    assert "no such image" in await tool.invoke({"path": "uploads/ghost.png"}, _ctx())
    assert "not a supported image" in await tool.invoke(
        {"path": "uploads/notes.txt"}, _ctx()
    )


def test_resolve_vision_tool_gating(tmp_path: Path) -> None:
    ws = Workspace.local(str(tmp_path))
    text_prov = ScriptedProvider([text("x")])  # no vision
    vision_prov = ScriptedProvider([text("x")])
    vision_prov.supports_vision = True  # type: ignore[attr-defined]
    profile = ModelProfile(id="eyes", model="openai:qwen-vl", api_key="sk-vision")

    assert resolve_vision_tool(text_prov, ws, None) is None  # no vision role
    assert resolve_vision_tool(text_prov, None, profile) is None  # no workspace
    # A vision-capable main model gets images inline — no delegation tool.
    assert resolve_vision_tool(vision_prov, ws, profile) is None
    tool = resolve_vision_tool(text_prov, ws, profile)
    assert tool is not None and tool.name == "see_image"

    # A vision model on its own endpoint: the overrides thread through cleanly.
    remote = ModelProfile(
        id="eyes2",
        model="openai:qwen-vl",
        base_url="https://dashscope.example/v1",
        api_key="sk-vision",
    )
    tool2 = resolve_vision_tool(text_prov, ws, remote)
    assert tool2 is not None and tool2.name == "see_image"


def test_resolve_vision_tool_bad_profile_degrades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An unusable vision profile disables the tool instead of crashing boot."""
    ws = Workspace.local(str(tmp_path))
    text_prov = ScriptedProvider([text("x")])
    bad = ModelProfile(id="eyes", model="no-such-vendor:qwen-vl")
    with caplog.at_level("WARNING", logger="lovia.web.builder"):
        assert resolve_vision_tool(text_prov, ws, bad) is None
    assert "see_image disabled" in caplog.text


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
