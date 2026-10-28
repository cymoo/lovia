"""``lovia web init-ui``: the starter UI directory for ``ChatUI(assets=...)``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import ChatUI, create_app  # noqa: E402
from lovia.web.__main__ import main as web_main  # noqa: E402
from lovia.web.scaffold import init_ui  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider  # noqa: E402


def test_writes_files_that_mirror_the_bundled_page(tmp_path: Path) -> None:
    written = init_ui(tmp_path / "ui")
    assert [p.name for p in written] == ["index.html", "theme.css", "extension.js"]

    page = (tmp_path / "ui" / "index.html").read_text()
    assert page.startswith('{% extends "lovia/index.html" %}')
    for block in ("head", "sidebar_footer", "welcome", "body_end"):
        assert f"{{% block {block} %}}" in page

    theme = (tmp_path / "ui" / "theme.css").read_text()
    # Every token commented out, so the scaffold alone changes nothing.
    assert not re.search(r"^\s*--", theme, re.M)
    # Colors per theme, shared tokens on :root.
    root, light, dark = re.findall(r"\{([^{}]*)\}", theme)
    assert "--lv-radius:" in root and "--lv-accent:" not in root
    assert "/* --lv-accent: #2d8a6e; */" in light
    assert "/* --lv-accent: #3ecb8f; */" in dark

    extension = (tmp_path / "ui" / "extension.js").read_text()
    ref = re.search(r'reference path="([^"]+)"', extension)
    assert (
        ref and Path(ref.group(1)).name == "lovia.d.ts" and Path(ref.group(1)).exists()
    )


def test_refuses_to_overwrite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "theme.css").write_text("mine")
    assert web_main(["init-ui", str(tmp_path / "ui")]) == 1
    assert "refusing to overwrite" in capsys.readouterr().err
    assert (tmp_path / "ui" / "theme.css").read_text() == "mine"
    assert not (tmp_path / "ui" / "index.html").exists()


def test_the_scaffold_serves_as_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert web_main(["init-ui", str(tmp_path / "ui")]) == 0
    assert "ChatUI(assets=" in capsys.readouterr().out
    ui = ChatUI(assets=tmp_path / "ui", scripts=["extension.js"], styles=["theme.css"])
    agent = Agent(name="bot", model=ScriptedProvider([]))
    app = create_app(agent, ui=ui, titles=False, store=ChatStore.in_memory())
    client = TestClient(app)
    page = client.get("/")
    assert page.status_code == 200
    assert "/ui/theme.css" in page.text and "/ui/extension.js" in page.text
    assert client.get("/ui/theme.css").status_code == 200
    assert client.get("/ui/extension.js").status_code == 200
