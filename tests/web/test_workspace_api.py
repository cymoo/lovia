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
from lovia.workspace import Workspace, WorkspaceLimits  # noqa: E402
from lovia.web import create_app  # noqa: E402
from lovia.web.store import ChatStore  # noqa: E402

from ..scripted_provider import ScriptedProvider, text  # noqa: E402

# A real PNG header so mimetypes + the binary sniff both see an image.
PNG_BYTES = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 32


def _seed(root: Path) -> None:
    (root / "notes").mkdir()
    (root / "notes" / "plan.md").write_text("# Plan\n\nhello **world**\n")
    (root / "report.csv").write_text("name,score\nada,99\ngrace,97\n")
    (root / ".secret").write_text("a dotfile, not a denied one")
    (root / "secrets.env").write_text("API_KEY=nope")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02 junk")
    (root / "pic.png").write_bytes(PNG_BYTES)
    (root / "big.txt").write_text("line\n" * 60_000)  # > max_file_read_chars
    # Environment junk: browsable, never in the change list (it isn't sent).
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00pyc")
    (root / "orphan.pyc").write_bytes(b"\x00pyc")
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "site.py").write_text("# fake venv content\n")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
    # The instructed scratch dir (workspace instructions send intermediates
    # here) — out of the change list, but browsable and readable: the agent
    # references what it writes there.
    (root / "tmp").mkdir()
    (root / "tmp" / "scratch.txt").write_text("half-done work\n")
    (root / "tmp" / "chart.png").write_bytes(PNG_BYTES)
    # A nested directory that merely shares the name is not the scratch dir.
    (root / "reports" / "tmp").mkdir(parents=True)
    (root / "reports" / "tmp" / "nested.txt").write_text("a deliverable\n")
    # Deterministic recency order: report.csv is the newest deliverable. The
    # junk is made newer still, so a list that walked the tree would show it.
    now = time.time()
    for i, name in enumerate(
        [
            "notes/plan.md",
            "reports/tmp/nested.txt",
            "big.txt",
            "pic.png",
            "blob.bin",
            "report.csv",
            "__pycache__/app.cpython-312.pyc",
            "orphan.pyc",
            "venv/bin/site.py",
            "node_modules/pkg/index.js",
            "tmp/scratch.txt",
            "tmp/chart.png",
        ]
    ):
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
        titles=False,
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
    assert info["scratch_dir"] == "tmp"  # the layout name the prompt teaches
    for ep in ("", "/files"):
        assert (
            client.get(f"/api/workspace{ep}", params={"agent": "plain"}).status_code
            == 404
        )
    res = client.post("/api/workspace/changes", params={"agent": "plain"}, json={})
    assert res.status_code == 404


def test_feature_flag_false_without_any_workspace() -> None:
    app = create_app(
        Agent(name="solo", model=ScriptedProvider([text("hi")])),
        store=ChatStore.in_memory(),
        titles=False,
    )
    c = TestClient(app)
    assert c.get("/api/info").json()["features"]["workspace"] is False
    assert c.get("/api/workspace").status_code == 404


# --------------------------------------------------------------- listing -


def test_files_lists_one_level_dirs_first_dotfiles_shown(
    client: TestClient, tmp_path: Path
) -> None:
    """Dotfiles list (the change list names them, and reveals them here);
    denied paths and ``.git`` don't."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    listing = client.get("/api/workspace/files", params={"agent": "bot"}).json()
    assert listing["truncated"] is False
    entries = listing["files"]
    paths = [e["path"] for e in entries]
    assert paths[0] == ".agents"  # dirs first
    assert "notes/plan.md" not in paths  # one level only
    assert ".secret" in paths
    assert ".git" not in paths
    assert "secrets.env" not in paths  # denied_paths
    sizes = {e["path"]: e["size"] for e in entries}
    assert sizes["report.csv"] > 0


def test_files_says_when_the_listing_cap_cut_it(tmp_path: Path) -> None:
    """A folder past ``max_list_results`` lists that many and says so: the
    Files panel's tree would otherwise show a short folder as complete."""
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    ws = Workspace.local(str(tmp_path), limits=WorkspaceLimits(max_list_results=3))
    app = create_app(
        {"bot": Agent(name="bot", model=ScriptedProvider([text("hi")]), workspace=ws)},
        store=ChatStore.in_memory(),
        titles=False,
    )
    listing = TestClient(app).get("/api/workspace/files", params={"agent": "bot"})
    assert listing.json()["truncated"] is True
    assert len(listing.json()["files"]) == 3


def test_files_subdir_and_missing(client: TestClient) -> None:
    entries = client.get(
        "/api/workspace/files", params={"agent": "bot", "path": "notes"}
    ).json()["files"]
    assert [e["path"] for e in entries] == ["notes/plan.md"]
    assert (
        client.get(
            "/api/workspace/files", params={"agent": "bot", "path": "nope"}
        ).status_code
        == 404
    )


def test_changes_lists_the_chats_files_that_are_still_there(
    client: TestClient, tmp_path: Path
) -> None:
    """Only the paths sent, each stat-ed — never a walk: gone, denied,
    outside, scratch and directories drop out, and two spellings of one
    file list once. The seeded junk is newer than all of them and absent."""
    paths = [
        "notes/plan.md",
        "./report.csv",
        str(tmp_path / "report.csv"),
        "reports/tmp/nested.txt",  # only the root's tmp/ is scratch
        "tmp/scratch.txt",
        "secrets.env",
        "/etc/hosts",
        "gone.md",
        "notes",
    ]
    res = client.post(
        "/api/workspace/changes", params={"agent": "bot"}, json={"paths": paths}
    ).json()
    assert res["git"] is False  # tmp_path is no repository
    assert [e["path"] for e in res["files"]] == [
        "report.csv",
        "reports/tmp/nested.txt",
        "notes/plan.md",
    ]  # newest first
    assert all(e["touched"] and e["status"] is None for e in res["files"])
    assert res["files"][0]["size"] > 0 and res["files"][0]["mtime"]
    too_many = {"paths": ["a"] * 501}
    assert (
        client.post(
            "/api/workspace/changes", json=too_many, params={"agent": "bot"}
        ).status_code
        == 422
    )


def test_browse_shows_what_changes_leaves_out(client: TestClient) -> None:
    """The change list's scratch rule is presentation only: a directory
    listing is one level and can't be crowded, so it tells the truth."""
    entries = client.get("/api/workspace/files", params={"agent": "bot"}).json()
    paths = {e["path"] for e in entries["files"]}
    assert {"__pycache__", "venv", "node_modules", "orphan.pyc", "tmp"} <= paths
    scratch = client.get(
        "/api/workspace/files", params={"agent": "bot", "path": "tmp"}
    ).json()["files"]
    assert {e["path"] for e in scratch} == {"tmp/chart.png", "tmp/scratch.txt"}


def test_skipped_paths_still_open_by_name(client: TestClient) -> None:
    """What the agent writes under tmp/ it also references — an inline image,
    a tool card's "open in Files" — and those must open, unlike the agent's
    own ``denied_paths``, which stay a real deny."""
    text = client.get(
        "/api/workspace/file", params={"agent": "bot", "path": "tmp/scratch.txt"}
    )
    assert text.status_code == 200
    assert "half-done" in text.json()["content"]
    image = client.get(
        "/api/workspace/raw", params={"agent": "bot", "path": "tmp/chart.png"}
    )
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    pyc = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "orphan.pyc", "download": 1},
    )
    assert pyc.status_code == 200
    denied = client.get(
        "/api/workspace/file", params={"agent": "bot", "path": "secrets.env"}
    )
    assert denied.status_code == 403


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
    root = Path(ws_app.state.deps.agents["bot"].workspace.root)
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
    # user-uploaded bytes served inline must not be MIME-sniffed
    assert r.headers["x-content-type-options"] == "nosniff"


def _put(ws_app, name: str, data: bytes) -> str:
    root = Path(ws_app.state.deps.agents["bot"].workspace.root)
    (root / name).write_bytes(data)
    return name


def test_raw_avif_uses_explicit_mime_not_os_guess(client: TestClient, ws_app) -> None:
    # AVIF isn't in every OS mime database; the explicit preview map must still
    # send a correct image Content-Type, or the nosniff'd inline preview breaks.
    path = _put(ws_app, "pic.avif", b"\x00\x00\x00\x1cftypavif")
    raw = client.get("/api/workspace/raw", params={"agent": "bot", "path": path})
    assert raw.status_code == 200
    assert raw.headers["content-type"] == "image/avif"
    assert raw.headers["x-content-type-options"] == "nosniff"


def test_raw_non_image_inline_refused(client: TestClient) -> None:
    r = client.get("/api/workspace/raw", params={"agent": "bot", "path": "report.csv"})
    assert r.status_code == 415


def test_raw_pdf_inline(client: TestClient, ws_app) -> None:
    # PDF joins images in the inline set (the Files panel embeds the browser's
    # sandboxed PDF viewer); the explicit media type keeps nosniff happy.
    path = _put(ws_app, "doc.pdf", b"%PDF-1.4 fake")
    r = client.get("/api/workspace/raw", params={"agent": "bot", "path": path})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "attachment" not in r.headers.get("content-disposition", "")


def test_raw_svg_inline_still_refused(client: TestClient, ws_app) -> None:
    # SVG can carry scripts — the PDF exception must not widen to it.
    path = _put(ws_app, "pic.svg", b"<svg xmlns='...'/>")
    r = client.get("/api/workspace/raw", params={"agent": "bot", "path": path})
    assert r.status_code == 415


def test_raw_svg_download_is_how_the_ui_renders_it(client: TestClient, ws_app) -> None:
    # The UI points an <img> at this exact response (workspaceImageUrl in
    # static/js/util.js): browsers ignore Content-Disposition for a subresource
    # — where SVG scripts don't run — and honor it for navigation, where they
    # would. Both headers are load-bearing, not cosmetic.
    path = _put(ws_app, "chart.svg", b"<svg xmlns='...'/>")
    r = client.get(
        "/api/workspace/raw", params={"agent": "bot", "path": path, "download": 1}
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.headers["content-disposition"].startswith("attachment")


def test_raw_download_any_file(client: TestClient) -> None:
    r = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "report.csv", "download": 1},
    )
    assert r.status_code == 200
    assert 'attachment; filename="report.csv"' in r.headers["content-disposition"]


def test_raw_revalidation_etag_304(client: TestClient, ws_app) -> None:
    # First fetch: revalidate-always caching (no-cache) with a validator.
    r = client.get("/api/workspace/raw", params={"agent": "bot", "path": "pic.png"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    etag = r.headers["etag"]
    assert etag

    # Unchanged file → 304, no body, validator retained.
    r2 = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "pic.png"},
        headers={"if-none-match": etag},
    )
    assert r2.status_code == 304
    assert r2.content == b""
    assert r2.headers["etag"] == etag

    # Weak-validator and multi-candidate forms browsers send still match.
    r3 = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "pic.png"},
        headers={"if-none-match": f'W/"nope", {etag}'},
    )
    assert r3.status_code == 304

    # "*" matches any current representation (RFC 9110 §13.1.2).
    r3b = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "pic.png"},
        headers={"if-none-match": "*"},
    )
    assert r3b.status_code == 304

    # Changed file → validator no longer matches, full bytes again.
    root = Path(ws_app.state.deps.agents["bot"].workspace.root)
    (root / "pic.png").write_bytes(PNG_BYTES + b"\x00")
    import os

    os.utime(root / "pic.png", (1, 1))  # force a different mtime
    r4 = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "pic.png"},
        headers={"if-none-match": etag},
    )
    assert r4.status_code == 200
    assert r4.headers["etag"] != etag


def test_raw_size_cap(client: TestClient, ws_app) -> None:
    root = Path(ws_app.state.deps.agents["bot"].workspace.root)
    limit = ws_app.state.deps.agents["bot"].workspace.limits.max_file_read_bytes
    (root / "huge.bin").write_bytes(b"x" * (limit + 1))
    r = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "huge.bin", "download": 1},
    )
    assert r.status_code == 413


# --------------------------------------------------- chat attachment guard -


def test_chat_rejects_request_whose_attachments_are_all_invalid(
    client: TestClient,
) -> None:
    # A traversal/missing attachment with no text must not bypass the empty
    # guard and start a blank run.
    for endpoint in ("/api/chat", "/api/chat/stream"):
        r = client.post(
            endpoint,
            json={
                "message": "",
                "agent": "bot",
                "attachments": [
                    {"path": "../../etc/passwd", "mime": "image/png", "kind": "image"}
                ],
            },
        )
        assert r.status_code == 422, endpoint


def test_chat_accepts_a_valid_attachment(client: TestClient, ws_app) -> None:
    root = Path(ws_app.state.deps.agents["bot"].workspace.root)
    (root / "uploads").mkdir(exist_ok=True)
    (root / "uploads" / "a.png").write_bytes(PNG_BYTES)
    r = client.post(
        "/api/chat",
        json={
            "message": "hi",
            "agent": "bot",
            "attachments": [
                {"path": "uploads/a.png", "mime": "image/png", "kind": "image"}
            ],
        },
    )
    assert r.status_code == 200
