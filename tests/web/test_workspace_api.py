"""Tests for the read-only /api/workspace endpoints (the Files panel).

First workspace-backed ``create_app`` fixture in the web suite: a real
``Workspace.local`` over ``tmp_path``, seeded with the shapes the endpoints
must handle — nested dirs, dotfiles, a binary, an oversized text, a
``denied_paths`` hit, and an image.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from lovia import Agent  # noqa: E402
from lovia.workspace import Workspace  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402

# A real PNG header so mimetypes + the binary sniff both see an image.
PNG_BYTES = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _seed(root: Path) -> None:
    (root / "notes").mkdir()
    (root / "notes" / "plan.md").write_text("# Plan\n\nhello **world**\n")
    (root / "report.csv").write_text("name,score\nada,99\ngrace,97\n")
    (root / ".secret").write_text("dotfile — hidden from listings")
    (root / "secrets.env").write_text("API_KEY=nope")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02 junk")
    (root / "pic.png").write_bytes(PNG_BYTES)
    (root / "big.txt").write_text("line\n" * 60_000)  # > max_file_read_chars
    # Deterministic recency order: report.csv is the newest file.
    now = time.time()
    for i, name in enumerate(
        ["notes/plan.md", "big.txt", "pic.png", "blob.bin", "report.csv"]
    ):
        (root / name).touch()
        import os

        os.utime(root / name, (now + i, now + i))


@pytest.fixture()
def ws_app(tmp_path: Path):
    _seed(tmp_path)
    ws = Workspace.local(str(tmp_path), mode="coding", denied_paths=("secrets.env",))
    bot = Agent(name="bot", model=ScriptedProvider([text("hi")]), workspace=ws)
    plain = Agent(name="plain", model=ScriptedProvider([text("hi")]))
    return create_app(
        {"bot": bot, "plain": plain},
        store=ChatStore.in_memory(),
        generate_titles=False,
    )


@pytest.fixture()
def client(ws_app) -> TestClient:
    return TestClient(ws_app)


# ------------------------------------------------------------- discovery -


def test_feature_flag_and_agent_info(client: TestClient) -> None:
    assert client.get("/api/info").json()["features"]["workspace"] is True
    agents = {a["name"]: a["workspace"] for a in client.get("/api/agents").json()}
    assert agents == {"bot": True, "plain": False}


def test_workspace_info_and_no_workspace_agent(client: TestClient) -> None:
    info = client.get("/api/workspace", params={"agent": "bot"}).json()
    assert info["name"]  # the root's directory name, never the full path
    assert "/" not in info["name"]
    for ep in ("", "/files", "/recent"):
        assert (
            client.get(f"/api/workspace{ep}", params={"agent": "plain"}).status_code
            == 404
        )


def test_feature_flag_false_without_any_workspace() -> None:
    app = create_app(
        Agent(name="solo", model=ScriptedProvider([text("hi")])),
        store=ChatStore.in_memory(),
        generate_titles=False,
    )
    c = TestClient(app)
    assert c.get("/api/info").json()["features"]["workspace"] is False
    assert c.get("/api/workspace").status_code == 404


# --------------------------------------------------------------- listing -


def test_files_lists_one_level_dirs_first_dotfiles_hidden(
    client: TestClient,
) -> None:
    entries = client.get("/api/workspace/files", params={"agent": "bot"}).json()
    paths = [e["path"] for e in entries]
    assert paths[0] == "notes"  # dirs first
    assert "notes/plan.md" not in paths  # one level only
    assert ".secret" not in paths  # dotfiles hidden
    sizes = {e["path"]: e["size"] for e in entries}
    assert sizes["report.csv"] > 0


def test_files_subdir_and_missing(client: TestClient) -> None:
    entries = client.get(
        "/api/workspace/files", params={"agent": "bot", "path": "notes"}
    ).json()
    assert [e["path"] for e in entries] == ["notes/plan.md"]
    assert (
        client.get(
            "/api/workspace/files", params={"agent": "bot", "path": "nope"}
        ).status_code
        == 404
    )


def test_recent_is_files_only_newest_first(client: TestClient) -> None:
    entries = client.get("/api/workspace/recent", params={"agent": "bot"}).json()
    assert all(not e["is_dir"] for e in entries)
    assert entries[0]["path"] == "report.csv"  # newest by seeded mtimes
    assert entries[-1]["path"] == "notes/plan.md"  # oldest
    limited = client.get(
        "/api/workspace/recent", params={"agent": "bot", "limit": 2}
    ).json()
    assert [e["path"] for e in limited] == ["report.csv", "blob.bin"]


# --------------------------------------------------------------- reading -


def test_file_content_markdown(client: TestClient) -> None:
    data = client.get(
        "/api/workspace/file", params={"agent": "bot", "path": "notes/plan.md"}
    ).json()
    assert data["content"].startswith("# Plan")
    assert data["binary"] is False
    assert data["truncated"] is False


def test_file_pagination_and_truncated(client: TestClient) -> None:
    first = client.get(
        "/api/workspace/file", params={"agent": "bot", "path": "big.txt"}
    ).json()
    assert first["truncated"] is True
    assert first["total_lines"] == 60_000
    nxt = client.get(
        "/api/workspace/file",
        params={"agent": "bot", "path": "big.txt", "start": first["end"] + 1},
    ).json()
    assert nxt["start"] == first["end"] + 1
    assert nxt["content"]


def test_file_binary_flagged_without_content(client: TestClient) -> None:
    data = client.get(
        "/api/workspace/file", params={"agent": "bot", "path": "blob.bin"}
    ).json()
    assert data["binary"] is True
    assert data["content"] == ""


def test_file_missing_and_directory(client: TestClient) -> None:
    for path in ("ghost.txt", "notes"):
        assert (
            client.get(
                "/api/workspace/file", params={"agent": "bot", "path": path}
            ).status_code
            == 404
        )


# -------------------------------------------------------------- security -


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", "~/x"])
def test_traversal_denied_everywhere(client: TestClient, path: str) -> None:
    for ep, params in (
        ("/api/workspace/files", {"path": path}),
        ("/api/workspace/file", {"path": path}),
        ("/api/workspace/raw", {"path": path, "download": 1}),
    ):
        r = client.get(ep, params={"agent": "bot", **params})
        assert r.status_code == 403, (ep, path, r.status_code)


def test_denied_paths_apply_to_the_panel(client: TestClient) -> None:
    r = client.get(
        "/api/workspace/file", params={"agent": "bot", "path": "secrets.env"}
    )
    assert r.status_code == 403
    r = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "secrets.env", "download": 1},
    )
    assert r.status_code == 403


def test_symlink_escaping_root_is_denied(client: TestClient, ws_app) -> None:
    root = Path(ws_app.state.agents["bot"].workspace.root)
    outside = root.parent / "outside.txt"
    outside.write_text("nope")
    (root / "sneaky.txt").symlink_to(outside)
    r = client.get("/api/workspace/file", params={"agent": "bot", "path": "sneaky.txt"})
    assert r.status_code == 403


# ------------------------------------------------------------------- raw -


def test_raw_image_inline(client: TestClient) -> None:
    r = client.get("/api/workspace/raw", params={"agent": "bot", "path": "pic.png"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG_BYTES
    assert "attachment" not in r.headers.get("content-disposition", "")


def test_raw_non_image_inline_refused(client: TestClient) -> None:
    r = client.get("/api/workspace/raw", params={"agent": "bot", "path": "report.csv"})
    assert r.status_code == 415


def test_raw_download_any_file(client: TestClient) -> None:
    r = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "report.csv", "download": 1},
    )
    assert r.status_code == 200
    assert 'attachment; filename="report.csv"' in r.headers["content-disposition"]


def test_raw_size_cap(client: TestClient, ws_app) -> None:
    root = Path(ws_app.state.agents["bot"].workspace.root)
    limit = ws_app.state.agents["bot"].workspace.limits.max_file_read_bytes
    (root / "huge.bin").write_bytes(b"x" * (limit + 1))
    r = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "huge.bin", "download": 1},
    )
    assert r.status_code == 413
