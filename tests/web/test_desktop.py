"""Show in Finder / open in editor: the launchers (``lovia.web.desktop``) and
the ``POST /api/workspace/reveal`` / ``edit`` routes in front of them.

Nothing here opens a window: the launchers are recorded, and the system
lookups (LaunchServices, ``AssocQueryStringW``) only run where they exist.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.web import WorkspaceSwitch, create_app, desktop  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402


@pytest.fixture()
def launched(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """What the launchers were asked to run."""
    calls: list[Any] = []
    monkeypatch.setattr(desktop, "_launch", calls.append)
    monkeypatch.setattr(desktop, "_launch_detached", calls.append)
    return calls


# ------------------------------------------------------------- launchers -


def test_macos_reveals_and_edits_without_running(
    monkeypatch: pytest.MonkeyPatch, launched: list[Any], tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    apps = {
        ".py": ("/Apps/PyCharm.app", "com.jetbrains.pycharm"),
        ".md": ("/Apps/Visual Studio Code.app", "com.microsoft.VSCode"),
        ".ts": ("/Apps/IINA.app", "com.colliderli.iina"),  # a video, to macOS
        ".command": ("/System/Terminal.app", "com.apple.Terminal"),
    }
    monkeypatch.setattr(desktop, "_mac_default_app", lambda p: apps.get(p.suffix))
    f = tmp_path / "a b.py"
    desktop.reveal(f)
    for name in ("a.py", "a.md", "a.ts", "a.command", "a.unknown"):
        desktop.edit(tmp_path / name)
    assert launched == [
        ["open", "-R", str(f)],
        ["open", "-a", "/Apps/PyCharm.app", str(tmp_path / "a.py")],
        ["open", "-a", "/Apps/Visual Studio Code.app", str(tmp_path / "a.md")],
        ["open", "-t", str(tmp_path / "a.ts")],
        ["open", "-t", str(tmp_path / "a.command")],
        ["open", "-t", str(tmp_path / "a.unknown")],
    ]


def test_windows_quotes_the_selection_and_falls_back_to_the_text_editor(
    monkeypatch: pytest.MonkeyPatch, launched: list[Any]
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assoc = {
        ".py": r"C:\Python\py.exe",  # runs the script: not an editor
        ".md": r"C:\VS Code\Code.exe",
        ".txt": r"C:\Notepad++\notepad++.exe",
    }
    monkeypatch.setattr(desktop, "_win_assoc", assoc.get)
    desktop.reveal(Path(r"C:\work\a,b.txt"))
    for name in ("x.py", "x.md", "Makefile"):
        desktop.edit(Path(name))
    monkeypatch.setattr(desktop, "_win_assoc", lambda ext: None)
    desktop.edit(Path("x.js"))
    assert launched == [
        r'explorer /select,"C:\work\a,b.txt"',
        [assoc[".txt"], "x.py"],
        [assoc[".md"], "x.md"],
        [assoc[".txt"], "Makefile"],
        ["notepad.exe", "x.js"],
    ]


def test_linux_opens_the_folder_and_has_no_editor(
    monkeypatch: pytest.MonkeyPatch, launched: list[Any], tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    (tmp_path / "f.txt").write_text("x")
    desktop.reveal(tmp_path / "f.txt")
    desktop.reveal(tmp_path)
    assert launched == [["xdg-open", str(tmp_path)], ["xdg-open", str(tmp_path)]]
    assert desktop.reveal_kind() == "folder" and not desktop.can_edit()
    with pytest.raises(desktop.LaunchError):
        desktop.edit(tmp_path / "f.txt")


def test_a_launcher_that_refuses_is_an_error() -> None:
    desktop._launch([sys.executable, "-c", "pass"])
    with pytest.raises(desktop.LaunchError, match="nope"):
        desktop._launch(
            [sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(1)"]
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="LaunchServices")
def test_macos_lookup_finds_the_text_app(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    found = desktop._mac_default_app(tmp_path / "a.txt")
    assert found is not None
    app, bundle_id = found
    assert app.endswith(".app") and "." in bundle_id


@pytest.mark.skipif(sys.platform != "win32", reason="AssocQueryStringW")
def test_windows_lookup_answers_for_text() -> None:
    exe = desktop._win_assoc(".txt")
    assert exe is None or (exe.lower().endswith(".exe") and Path(exe).is_file())
    assert desktop._win_assoc(".no-such-type-here") is None
    assert desktop._win_editor_for(Path("x.py")).lower().endswith(".exe")


# ---------------------------------------------------------------- routes -


def _agent(root: Path) -> Agent:
    ws = Workspace.local(str(root), mode="coding", denied_paths=("secrets.env",))
    return Agent(name="bot", model=ScriptedProvider([text("hi")]), workspace=ws)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print(1)\n")
    (root / "secrets.env").write_text("KEY=1")
    (tmp_path / "outside.txt").write_text("not the project's")
    try:
        (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    except OSError:  # Windows without the symlink privilege
        pass
    return root


@pytest.fixture()
def opened(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(desktop, "reveal", lambda p: calls.append(("reveal", p)))
    monkeypatch.setattr(desktop, "edit", lambda p: calls.append(("edit", p)))
    monkeypatch.setattr(desktop, "can_edit", lambda: True)
    return calls


def _local(app: Any) -> TestClient:
    return TestClient(app, base_url="http://localhost:8000")


def test_reveal_and_edit_act_on_the_file_inside_the_workspace(
    root: Path, opened: list[tuple[str, Path]]
) -> None:
    c = _local(create_app(_agent(root), store=ChatStore.in_memory(), titles=False))
    info = c.get("/api/info").json()["desktop"]
    assert info["reveal"] in ("finder", "explorer", "folder")
    assert c.post("/api/workspace/reveal", json={"path": "src"}).status_code == 204
    assert c.post("/api/workspace/reveal", json={}).status_code == 204  # the root
    assert c.post("/api/workspace/edit", json={"path": "src/app.py"}).status_code == 204
    real = root.resolve()
    assert opened == [
        ("reveal", real / "src"),
        ("reveal", real),
        ("edit", real / "src" / "app.py"),
    ]
    assert c.post("/api/workspace/edit", json={"path": "src"}).status_code == 400


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("secrets.env", 403),  # denied, as in the panel
        ("../outside.txt", 403),  # outside the root
        ("link.txt", 403),  # a link judged by where it lands
        ("nope.txt", 404),
    ],
)
def test_what_the_panel_cannot_read_is_not_opened(
    root: Path, opened: list[tuple[str, Path]], path: str, status: int
) -> None:
    if path == "link.txt" and not (root / path).is_symlink():
        pytest.skip("no symlinks here")
    c = _local(create_app(_agent(root), store=ChatStore.in_memory(), titles=False))
    for action in ("reveal", "edit"):
        assert c.post(f"/api/workspace/{action}", json={"path": path}).status_code == (
            status
        )
    assert opened == []


def test_only_a_browser_on_this_machine(
    root: Path, opened: list[tuple[str, Path]]
) -> None:
    app = create_app(_agent(root), store=ChatStore.in_memory(), titles=False)
    body = {"path": "src/app.py"}
    rebound = TestClient(app, base_url="http://evil.example")  # DNS rebinding
    assert rebound.post("/api/workspace/reveal", json=body).status_code == 403
    c = _local(app)
    foreign = {"origin": "https://evil.example"}
    assert c.post("/api/workspace/edit", json=body, headers=foreign).status_code == 403
    same = {"origin": "http://127.0.0.1:8000"}
    assert c.post("/api/workspace/edit", json=body, headers=same).status_code == 204
    assert [a for a, _ in opened] == ["edit"]


def test_a_guarded_server_has_no_desktop_routes(
    root: Path, opened: list[tuple[str, Path]]
) -> None:
    app = create_app(
        _agent(root), store=ChatStore.in_memory(), titles=False, token="s3cret"
    )
    c = _local(app)
    auth = {"authorization": "Bearer s3cret"}
    assert c.get("/api/info", headers=auth).json()["desktop"] is None
    res = c.post("/api/workspace/reveal", json={"path": "src"}, headers=auth)
    assert res.status_code == 404
    assert opened == []


def test_a_sidebar_project_is_named_by_its_root(
    root: Path, tmp_path: Path, opened: list[tuple[str, Path]]
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    app = create_app(
        _agent(root),
        store=ChatStore.in_memory(),
        titles=False,
        workspaces=WorkspaceSwitch(),
    )
    c = _local(app)
    res = c.post("/api/workspace/reveal", params={"root": str(other)}, json={})
    assert res.status_code == 204
    assert opened == [("reveal", other.resolve())]


def test_a_launcher_failure_is_reported(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_: Path) -> None:
        raise desktop.LaunchError("no application knows this file")

    monkeypatch.setattr(desktop, "reveal", refuse)
    c = _local(create_app(_agent(root), store=ChatStore.in_memory(), titles=False))
    res = c.post("/api/workspace/reveal", json={"path": "src"})
    assert res.status_code == 502
    assert res.json()["detail"]["code"] == "launch_failed"
