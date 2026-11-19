"""The host side of a remote chat's attachments (``remote/serve.py``).

A host keeps copies of chats' attachments under its temp directory, in a
folder its account alone owns — POSIX ``/tmp`` is shared, so a folder someone
else made first, or a link planted in its place, must never receive them.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from lovia.workspace.remote import serve

CHAT = "ab" * 16


@pytest.fixture
def host_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "tmp"
    where.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(where))
    return where.resolve()


def _account(host_tmp: Path) -> Path:
    return host_tmp / ("lovia" if os.name != "posix" else f"lovia-{os.getuid()}")


def test_copies_are_listed_with_their_size_and_mtime(host_tmp: Path) -> None:
    listed = serve.attachments_list(CHAT)
    assert listed == {
        "folder": (_account(host_tmp) / "attachments" / CHAT).as_posix(),
        "files": {},
        "refused": None,
    }
    folder = listed["folder"]
    serve.attachments_put(CHAT, "a.txt", b"hello", 1_700_000_000.5)
    # A stale copy is replaced, stamped with the original's mtime.
    serve.attachments_put(CHAT, "a.txt", b"hi", 1_700_000_100.9)
    assert serve.attachments_list(CHAT)["files"] == {"a.txt": [2, 1_700_000_100]}
    assert (Path(folder) / "a.txt").read_bytes() == b"hi"
    assert [p.name for p in Path(folder).iterdir()] == ["a.txt"]  # no temp left


def test_a_puts_leftover_is_no_copy(host_tmp: Path) -> None:
    # A put a dropped host never finished: never listed, removed once old;
    # one still being written is left alone.
    serve.attachments_put(CHAT, "a.txt", b"x", 1_700_000_000)
    folder = _account(host_tmp) / "attachments" / CHAT
    (folder / ".put-old").write_bytes(b"half")
    os.utime(folder / ".put-old", (1, 1))
    (folder / ".put-now").write_bytes(b"half")
    assert serve.attachments_list(CHAT)["files"] == {"a.txt": [1, 1_700_000_000]}
    assert sorted(p.name for p in folder.iterdir()) == [".put-now", "a.txt"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_account_folder_is_private(host_tmp: Path) -> None:
    serve.attachments_put(CHAT, "a.txt", b"x", 0)
    for path in (_account(host_tmp), _account(host_tmp) / "attachments"):
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX temp-dir ownership")
def test_a_folder_others_can_enter_is_refused(host_tmp: Path) -> None:
    account = _account(host_tmp)
    account.mkdir(mode=0o755)
    account.chmod(0o755)
    with pytest.raises(PermissionError, match="not a private directory"):
        serve.attachments_put(CHAT, "a.txt", b"x", 0)
    assert not (account / "attachments").exists()
    # Still named, with why: what sits there must stay guarded.
    listed = serve.attachments_list(CHAT)
    assert listed["folder"] == (account / "attachments" / CHAT).as_posix()
    assert listed["files"] == {} and "not a private directory" in listed["refused"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX temp-dir ownership")
def test_a_link_in_the_folders_place_is_refused(host_tmp: Path) -> None:
    elsewhere = host_tmp / "someone-elses"
    elsewhere.mkdir(mode=0o700)
    _account(host_tmp).symlink_to(elsewhere, target_is_directory=True)
    assert serve.attachments_list(CHAT)["refused"]
    with pytest.raises(PermissionError):
        serve.attachments_put(CHAT, "a.txt", b"x", 0)
    assert list(elsewhere.iterdir()) == []


def test_a_listing_during_a_put_leaves_it_be(
    host_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The copy is stamped with an old mtime; its temp file must not carry it
    # while a listing could take it for one a dropped host abandoned.
    real = os.replace

    def replace_after_a_listing(src, dst):
        assert serve.attachments_list(CHAT)["files"] == {}
        real(src, dst)

    with monkeypatch.context() as patched:
        patched.setattr(os, "replace", replace_after_a_listing)
        serve.attachments_put(CHAT, "a.txt", b"x", 1)
    assert serve.attachments_list(CHAT)["files"] == {"a.txt": [1, 1]}


@pytest.mark.parametrize("chat", ["../x", "AB" * 16, "ab" * 15, ""])
def test_only_a_hex_id_names_a_folder(host_tmp: Path, chat: str) -> None:
    with pytest.raises(ValueError):
        serve.attachments_list(chat)


@pytest.mark.parametrize("name", ["", ".", "..", "../x.txt", "a/b", "a\\b", "a\0b"])
def test_only_a_plain_name_is_written(host_tmp: Path, name: str) -> None:
    with pytest.raises(ValueError):
        serve.attachments_put(CHAT, name, b"x", 0)
