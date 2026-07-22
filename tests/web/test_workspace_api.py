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
    # Environment junk the panel must hide (see _PANEL_IGNORES).
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00pyc")
    (root / "orphan.pyc").write_bytes(b"\x00pyc")
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "site.py").write_text("# fake venv content\n")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
    # Deterministic recency order: report.csv is the newest file. The junk is
    # made newer still, so if the panel filter broke it would visibly take
    # over the top of Recent.
    now = time.time()
    for i, name in enumerate(
        [
            "notes/plan.md",
            "big.txt",
            "pic.png",
            "blob.bin",
            "report.csv",
            "__pycache__/app.cpython-312.pyc",
            "orphan.pyc",
            "venv/bin/site.py",
            "node_modules/pkg/index.js",
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


# The seeded junk (all newer than report.csv — it would top Recent if the
# filter broke) must be invisible in every panel view: Recent, browsing,
# preview, and download alike.


def test_recent_hides_environment_junk(client: TestClient) -> None:
    entries = client.get("/api/workspace/recent", params={"agent": "bot"}).json()
    paths = [e["path"] for e in entries]
    assert "report.csv" in paths  # real files still there
    for path in paths:
        assert not path.endswith(".pyc")
        assert not path.startswith(("venv/", "node_modules/", "__pycache__/"))


def test_browse_hides_junk_dirs(client: TestClient) -> None:
    entries = client.get("/api/workspace/files", params={"agent": "bot"}).json()
    paths = {e["path"] for e in entries}
    assert paths.isdisjoint({"__pycache__", "venv", "node_modules", "orphan.pyc"})


def test_junk_paths_refused_like_denied_ones(client: TestClient) -> None:
    for ep, params in (
        ("/api/workspace/files", {"path": "__pycache__"}),
        ("/api/workspace/file", {"path": "__pycache__/app.cpython-312.pyc"}),
        ("/api/workspace/raw", {"path": "orphan.pyc", "download": 1}),
    ):
        r = client.get(ep, params={"agent": "bot", **params})
        assert r.status_code == 403, (ep, params, r.status_code)


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
    # user-uploaded bytes served inline must not be MIME-sniffed
    assert r.headers["x-content-type-options"] == "nosniff"


def test_raw_avif_uses_explicit_mime_not_os_guess(client: TestClient) -> None:
    # AVIF isn't in every OS mime database; the explicit preview map must still
    # send a correct image Content-Type, or the nosniff'd inline preview breaks.
    up = client.post(
        "/api/workspace/upload",
        params={"agent": "bot"},
        files={"file": ("pic.avif", b"\x00\x00\x00\x1cftypavif", "application/octet-stream")},
    )
    assert up.status_code == 200
    assert up.json()["kind"] == "image"
    assert up.json()["mime"] == "image/avif"
    raw = client.get(
        "/api/workspace/raw", params={"agent": "bot", "path": up.json()["path"]}
    )
    assert raw.status_code == 200
    assert raw.headers["content-type"] == "image/avif"
    assert raw.headers["x-content-type-options"] == "nosniff"


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
    root = Path(ws_app.state.agents["bot"].workspace.root)
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
    root = Path(ws_app.state.agents["bot"].workspace.root)
    limit = ws_app.state.agents["bot"].workspace.limits.max_file_read_bytes
    (root / "huge.bin").write_bytes(b"x" * (limit + 1))
    r = client.get(
        "/api/workspace/raw",
        params={"agent": "bot", "path": "huge.bin", "download": 1},
    )
    assert r.status_code == 413


# --------------------------------------------------------------- upload -


def test_upload_writes_to_uploads_and_serves_back(client: TestClient, ws_app) -> None:
    root = Path(ws_app.state.agents["bot"].workspace.root)
    r = client.post(
        "/api/workspace/upload",
        params={"agent": "bot"},
        files={"file": ("cat.png", PNG_BYTES, "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "uploads/cat.png"
    assert body["kind"] == "image"
    assert body["mime"] == "image/png"
    assert body["size"] == len(PNG_BYTES)
    assert (root / body["path"]).is_file()
    # Immediately servable via the existing raw endpoint (inline image).
    raw = client.get(
        "/api/workspace/raw", params={"agent": "bot", "path": body["path"]}
    )
    assert raw.status_code == 200
    assert raw.content == PNG_BYTES


def test_upload_requires_a_workspace(client: TestClient) -> None:
    r = client.post(
        "/api/workspace/upload",
        params={"agent": "plain"},
        files={"file": ("x.txt", b"hi", "text/plain")},
    )
    assert r.status_code == 404


def test_upload_sanitizes_filename_no_traversal(client: TestClient, ws_app) -> None:
    root = Path(ws_app.state.agents["bot"].workspace.root)
    r = client.post(
        "/api/workspace/upload",
        params={"agent": "bot"},
        files={"file": ("../../evil.txt", b"data", "text/plain")},
    )
    assert r.status_code == 200
    path = r.json()["path"]
    assert path.startswith("uploads/") and ".." not in path
    target = (root / path).resolve()
    assert (root / "uploads").resolve() in target.parents


def test_upload_dedupes_name_collisions(client: TestClient) -> None:
    def send() -> str:
        return client.post(
            "/api/workspace/upload",
            params={"agent": "bot"},
            files={"file": ("dup.txt", b"data", "text/plain")},
        ).json()["path"]

    assert send() != send()


def test_upload_rejects_empty_file(client: TestClient) -> None:
    r = client.post(
        "/api/workspace/upload",
        params={"agent": "bot"},
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert r.status_code == 422


def test_upload_svg_is_not_treated_as_inline_image(client: TestClient) -> None:
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    r = client.post(
        "/api/workspace/upload",
        params={"agent": "bot"},
        files={"file": ("diagram.svg", svg, "image/svg+xml")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "file"  # svg is not an inline-safe raster image
    path = body["path"]
    # The raw endpoint refuses to serve it inline (would be stored XSS)...
    assert (
        client.get(
            "/api/workspace/raw", params={"agent": "bot", "path": path}
        ).status_code
        == 415
    )
    # ...but it is still downloadable.
    dl = client.get(
        "/api/workspace/raw", params={"agent": "bot", "path": path, "download": 1}
    )
    assert dl.status_code == 200


def test_upload_rejects_disallowed_extension(client: TestClient) -> None:
    r = client.post(
        "/api/workspace/upload",
        params={"agent": "bot"},
        files={"file": ("payload.exe", b"MZ\x00\x00", "application/octet-stream")},
    )
    assert r.status_code == 415


def test_upload_allowlist_can_be_overridden(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A narrow allowlist rejects an otherwise-common type...
    monkeypatch.setenv("LOVIA_UPLOAD_ALLOWED_EXT", "png, jpg")
    assert (
        client.post(
            "/api/workspace/upload",
            params={"agent": "bot"},
            files={"file": ("notes.txt", b"hi", "text/plain")},
        ).status_code
        == 415
    )
    # ...and "*" opens it back up to anything.
    monkeypatch.setenv("LOVIA_UPLOAD_ALLOWED_EXT", "*")
    assert (
        client.post(
            "/api/workspace/upload",
            params={"agent": "bot"},
            files={"file": ("weird.xyz", b"data", "application/octet-stream")},
        ).status_code
        == 200
    )


def test_upload_extensionless_file_is_allowed(client: TestClient) -> None:
    # No extension (README, Makefile, LICENSE) → not rejected by the allowlist.
    r = client.post(
        "/api/workspace/upload",
        params={"agent": "bot"},
        files={"file": ("Makefile", b"all:\n\techo hi\n", "text/plain")},
    )
    assert r.status_code == 200
    assert r.json()["kind"] == "file"


def test_upload_size_cap_from_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOVIA_MAX_UPLOAD_MB", "1")
    oversize = b"x" * (1024 * 1024 + 1)
    r = client.post(
        "/api/workspace/upload",
        params={"agent": "bot"},
        files={"file": ("big.txt", oversize, "text/plain")},
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
    root = Path(ws_app.state.agents["bot"].workspace.root)
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
