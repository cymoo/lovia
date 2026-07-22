"""The see_image tool (vision-as-a-tool) and its CLI env gating.

The tool lets a text-only main model delegate "look at this image" to a vision
model, reading only workspace files. Gating: registered only when a vision model
is configured, a workspace exists, and the main model can't already see images.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lovia.run_context import RunContext
from lovia.web.__main__ import resolve_vision_tool
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


def test_resolve_vision_tool_gating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = Workspace.local(str(tmp_path))
    text_prov = ScriptedProvider([text("x")])  # no vision
    vision_prov = ScriptedProvider([text("x")])
    vision_prov.supports_vision = True  # type: ignore[attr-defined]

    monkeypatch.delenv("LOVIA_VISION_MODEL", raising=False)
    assert resolve_vision_tool(text_prov, ws) is None  # not configured

    monkeypatch.setenv("LOVIA_VISION_MODEL", "openai:qwen-vl")
    assert resolve_vision_tool(text_prov, None) is None  # no workspace
    assert resolve_vision_tool(vision_prov, ws) is None  # main model already sees
    tool = resolve_vision_tool(text_prov, ws)  # text main + workspace + configured
    assert tool is not None and tool.name == "see_image"

    # A vision model on its own endpoint: the overrides thread through cleanly.
    monkeypatch.setenv("LOVIA_VISION_BASE_URL", "https://dashscope.example/v1")
    monkeypatch.setenv("LOVIA_VISION_API_KEY", "sk-vision")
    assert resolve_vision_tool(text_prov, ws).name == "see_image"


def test_env_bool_parses_values_and_guards_model_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lovia.web.__main__ import _env_bool

    monkeypatch.delenv("X_FLAG", raising=False)
    assert _env_bool("X_FLAG") is None  # unset
    monkeypatch.setenv("X_FLAG", "1")
    assert _env_bool("X_FLAG") is True
    monkeypatch.setenv("X_FLAG", "off")
    assert _env_bool("X_FLAG") is False
    # The footgun (a model spec in a boolean flag) reads false, not truthy.
    monkeypatch.setenv("X_FLAG", "openai:qwen-vl")
    assert _env_bool("X_FLAG") is False
