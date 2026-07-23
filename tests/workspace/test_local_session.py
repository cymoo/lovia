"""LocalWorkspaceSession behavior: path safety, files, search, shell."""

from __future__ import annotations

import asyncio
import gc
import os

import pytest

from lovia.exceptions import UserError
from lovia.workspace import local as local_module
from lovia.workspace import (
    CommandRule,
    LocalWorkspaceSession,
    PathRule,
    PermissionDeniedError,
    Workspace,
    WorkspaceClosedError,
    WorkspaceError,
    WorkspaceLimits,
    WorkspacePolicy,
)


async def _session(tmp_path, **kwargs) -> LocalWorkspaceSession:
    return LocalWorkspaceSession(root=str(tmp_path), **kwargs)


# ---------------------------------------------------------------------------
# Path ACL enforcement
# ---------------------------------------------------------------------------


async def test_outside_paths_denied_by_default_policy(tmp_path) -> None:
    # The bare WorkspacePolicy() defaults are conservative: reads and writes
    # outside the root are denied (coding softens reads to "ask").
    session = await _session(tmp_path)
    with pytest.raises(PermissionDeniedError, match="outside the workspace"):
        await session.read_text("/etc/passwd")
    with pytest.raises(PermissionDeniedError):
        await session.read_text("../outside.txt")
    with pytest.raises(PermissionDeniedError):
        await session.write_text("a/../../escape.txt", "x")


async def test_absolute_path_inside_root_is_accepted(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    session = await _session(tmp_path)
    result = await session.read_text(str(tmp_path / "a.txt"))
    assert result.content == "hi"
    assert result.path == "a.txt"  # reported workspace-relative


async def test_symlink_is_judged_by_its_target(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    # Default policy: the target is outside -> denied.
    session = await _session(tmp_path)
    with pytest.raises(PermissionDeniedError):
        await session.read_text("link.txt")

    # A policy that grants the target's location makes the same link readable.
    granted = await _session(
        tmp_path,
        policy=WorkspacePolicy(
            path_rules=(PathRule(outside.parent.as_posix(), "allow", ops=("read",)),)
        ),
    )
    assert (await granted.read_text("link.txt")).content == "secret"

    # trusted reads anywhere, so the link works there too.
    trusted = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    assert (await trusted.read_text("link.txt")).content == "secret"


async def test_session_decide_path_reports_ask_for_outside_reads(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.coding())
    assert session.decide_path("/etc/hosts") == "ask"
    assert session.decide_path("inside.txt") == "allow"
    assert session.decide_path("/etc/hosts", write=True) == "deny"
    # "ask" passes at the session level: approval is gated by the tool layer.
    (tmp_path.parent / "shared.txt").write_text("ok", encoding="utf-8")
    result = await session.read_text((tmp_path.parent / "shared.txt").as_posix())
    assert result.content == "ok"


async def test_closed_session_refuses_operations(tmp_path) -> None:
    session = await _session(tmp_path)
    await session.close()
    with pytest.raises(WorkspaceClosedError):
        await session.read_text("x.txt")


# ---------------------------------------------------------------------------
# Policy enforcement at the session level
# ---------------------------------------------------------------------------


async def test_readonly_policy_blocks_writes_and_edits(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    session = await _session(tmp_path, policy=WorkspacePolicy.readonly())
    assert (await session.read_text("a.txt")).content == "hi"
    with pytest.raises(PermissionDeniedError):
        await session.write_text("a.txt", "new")
    with pytest.raises(PermissionDeniedError):
        await session.edit_text("a.txt", "hi", "bye")


async def test_denied_paths_are_unreadable_and_hidden(tmp_path) -> None:
    (tmp_path / ".env").write_text("KEY=1", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('SECRET_REF')", encoding="utf-8")
    policy = WorkspacePolicy(denied_paths=(".env",))
    session = await _session(tmp_path, policy=policy)
    with pytest.raises(PermissionDeniedError):
        await session.read_text(".env")
    listed = await session.list_files(".", include_hidden=True)
    assert ".env" not in [e.path for e in listed]
    matches = await session.grep("KEY", path=".")
    assert matches == []


async def test_session_run_refuses_denied_commands(tmp_path) -> None:
    session = await _session(
        tmp_path,
        policy=WorkspacePolicy.trusted(command_rules=(CommandRule("rm", "deny"),)),
    )
    with pytest.raises(PermissionDeniedError):
        await session.run("rm -rf .")


# ---------------------------------------------------------------------------
# Read / write / edit
# ---------------------------------------------------------------------------


async def test_read_with_line_ranges_and_truncation(tmp_path) -> None:
    (tmp_path / "big.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 101)), encoding="utf-8"
    )
    session = await _session(tmp_path, limits=WorkspaceLimits(max_file_read_chars=30))
    page = await session.read_text("big.txt", start=2, end=3)
    assert page.content == "line2\nline3\n"
    assert page.start == 2 and page.end == 3 and page.total_lines == 100
    assert page.truncated is True  # there are more lines after the range

    clipped = await session.read_text("big.txt")
    assert clipped.truncated is True
    assert "truncated" in clipped.content


async def test_read_guards_against_oversized_files(tmp_path) -> None:
    (tmp_path / "huge.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 2001)), encoding="utf-8"
    )
    # A tiny byte cap forces the oversized path without a real huge file.
    session = await _session(tmp_path, limits=WorkspaceLimits(max_file_read_bytes=200))
    result = await session.read_text("huge.txt")
    assert result.truncated is True
    # Only a bounded prefix was read, so far fewer than the 2000 real lines.
    assert 0 < result.total_lines < 2000


async def test_read_oversized_appends_note_when_not_char_clipped(tmp_path) -> None:
    # The byte cap cuts the file, but the prefix fits under the char cap so
    # clip_text adds nothing — read_text must still flag the partial read.
    (tmp_path / "huge.txt").write_text(
        "\n".join(f"line{i}" for i in range(1, 2001)), encoding="utf-8"
    )
    session = await _session(tmp_path, limits=WorkspaceLimits(max_file_read_bytes=200))
    result = await session.read_text("huge.txt")
    assert result.truncated is True
    assert "leading portion" in result.content


async def test_read_past_eof_is_empty_not_backwards(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    session = await _session(tmp_path)
    result = await session.read_text("f.txt", start=10)
    assert result.content == "" and result.total_lines == 3 and result.start == 10


async def test_read_empty_file(tmp_path) -> None:
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    session = await _session(tmp_path)
    result = await session.read_text("empty.txt")
    assert result.content == "" and result.total_lines == 0


async def test_read_file_without_trailing_newline(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("one\ntwo", encoding="utf-8")  # no final newline
    session = await _session(tmp_path)
    result = await session.read_text("f.txt")
    assert result.total_lines == 2 and result.content == "one\ntwo"
    assert result.truncated is False


async def test_read_end_beyond_total_clamps(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    session = await _session(tmp_path)
    result = await session.read_text("f.txt", start=1, end=999)
    assert result.content == "a\nb\nc\n" and result.end == 3
    assert result.truncated is False


async def test_read_invalid_ranges_raise(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\n", encoding="utf-8")
    session = await _session(tmp_path)
    for kwargs in ({"start": 0}, {"end": 0}, {"start": 3, "end": 2}):
        with pytest.raises(WorkspaceError):
            await session.read_text("f.txt", **kwargs)  # type: ignore[arg-type]


async def test_read_directory_raises(tmp_path) -> None:
    (tmp_path / "dir").mkdir()
    session = await _session(tmp_path)
    with pytest.raises(WorkspaceError, match="Not a file"):
        await session.read_text("dir")


async def test_write_create_only_and_nested_dirs(tmp_path) -> None:
    session = await _session(tmp_path)
    created = await session.write_text("a/b/new.txt", "data", create_only=True)
    assert created.action == "created"
    blocked = await session.write_text("a/b/new.txt", "other", create_only=True)
    assert blocked.ok is False and blocked.action == "unchanged"


async def test_edit_exact_replace_and_failures(tmp_path) -> None:
    (tmp_path / "code.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
    session = await _session(tmp_path)

    missing = await session.edit_text("code.py", "z = 9", "z = 10")
    assert missing.ok is False and "not found" in (missing.message or "")

    ambiguous = await session.edit_text("code.py", "= 1", "= 2")
    assert ambiguous.ok is False and ambiguous.replacements == 2
    assert "replace_all" in (ambiguous.message or "")

    everywhere = await session.edit_text("code.py", "= 1", "= 2", replace_all=True)
    assert everywhere.ok is True and everywhere.replacements == 2
    assert (tmp_path / "code.py").read_text() == "x = 2\ny = 2\n"

    single = await session.edit_text("code.py", "x = 2", "x = 3")
    assert single.ok and single.changed and single.replacements == 1


async def test_edit_empty_old_is_rejected(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    session = await _session(tmp_path)
    result = await session.edit_text("a.txt", "", "new")
    assert result.ok is False and "must not be empty" in (result.message or "")


async def test_edit_noop_when_old_equals_new(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    session = await _session(tmp_path)
    result = await session.edit_text("a.txt", "hello", "hello")
    assert result.ok is True and result.changed is False and result.replacements == 1


async def test_edit_refuses_non_utf8_and_leaves_bytes_intact(tmp_path) -> None:
    raw = b"caf\xe9 = 1\n"  # lone 0xe9 is not valid UTF-8
    (tmp_path / "bin.txt").write_bytes(raw)
    session = await _session(tmp_path)
    result = await session.edit_text("bin.txt", "= 1", "= 2")
    assert result.ok is False and "UTF-8" in (result.message or "")
    # The original bytes must be untouched, not rewritten with U+FFFD.
    assert (tmp_path / "bin.txt").read_bytes() == raw


async def test_write_to_root_is_rejected(tmp_path) -> None:
    session = await _session(tmp_path)
    with pytest.raises(WorkspaceError, match="root as a file"):
        await session.write_text(".", "data")


async def test_write_overwrite_and_unicode_roundtrip(tmp_path) -> None:
    session = await _session(tmp_path)
    first = await session.write_text("f.txt", "one")
    assert first.action == "created"
    second = await session.write_text("f.txt", "二 🚀")
    assert second.action == "updated"
    assert (await session.read_text("f.txt")).content == "二 🚀"


# ---------------------------------------------------------------------------
# list_files: plain listing and glob mode
# ---------------------------------------------------------------------------


async def test_list_children_sorted_dirs_first(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")
    session = await _session(tmp_path)
    entries = await session.list_files(".")
    assert [(e.path, e.is_dir) for e in entries] == [("src", True), ("b.txt", False)]
    with_hidden = await session.list_files(".", include_hidden=True)
    assert ".hidden" in [e.path for e in with_hidden]


async def test_list_with_glob_pattern(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "top.py").write_text("t", encoding="utf-8")
    session = await _session(tmp_path)
    entries = await session.list_files(".", pattern="**/*.py")
    assert [e.path for e in entries] == ["src/a.py", "top.py"]


async def test_list_glob_too_many_matches_truncates(tmp_path) -> None:
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    session = await _session(tmp_path)
    matched = await session.list_files(".", pattern="*.txt", max_results=3)
    # Truncate-and-flag rather than raising, so the model still gets results.
    assert len(matched) == 3
    assert getattr(matched, "truncated", False) is True


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


async def test_grep_finds_lines_with_metadata(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def foo():\n    return 42\n", encoding="utf-8"
    )
    (tmp_path / "notes.md").write_text("Returns nothing.\n", encoding="utf-8")
    session = await _session(tmp_path)

    matches = await session.grep("return", ignore_case=False)
    assert [(m.path, m.line) for m in matches] == [("src/app.py", 2)]

    ci = await session.grep("return", ignore_case=True)
    assert {m.path for m in ci} == {"src/app.py", "notes.md"}

    only_md = await session.grep("return", glob="*.md", ignore_case=True)
    assert [m.path for m in only_md] == ["notes.md"]


async def test_grep_skips_binary_and_caps_matches(tmp_path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"return\0binary")
    (tmp_path / "many.txt").write_text("hit\n" * 50, encoding="utf-8")
    session = await _session(tmp_path)
    assert await session.grep("return") == []
    capped = await session.grep("hit", max_matches=10)
    assert len(capped) == 10


async def test_grep_invalid_regex_raises(tmp_path) -> None:
    session = await _session(tmp_path)
    with pytest.raises(WorkspaceError, match="Invalid regular expression"):
        await session.grep("(unclosed")


async def test_list_truncates_instead_of_raising(tmp_path) -> None:
    for i in range(10):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    session = await _session(tmp_path, limits=WorkspaceLimits(max_list_results=4))
    children = await session.list_files(".")
    assert len(children) == 4  # capped, not an error
    assert getattr(children, "truncated", False) is True
    matched = await session.list_files(".", pattern="*.txt")
    assert len(matched) == 4
    assert getattr(matched, "truncated", False) is True


async def test_grep_include_hidden_and_skips_escaping_symlink(tmp_path) -> None:
    (tmp_path / ".secret.txt").write_text("token", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("token", encoding="utf-8")
    session = await _session(tmp_path)
    assert [m.path for m in await session.grep("token")] == ["visible.txt"]
    incl = await session.grep("token", include_hidden=True)
    assert {m.path for m in incl} == {".secret.txt", "visible.txt"}

    # A symlinked file pointing outside the root is not searched through.
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("token", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    assert "link.txt" not in {m.path for m in await session.grep("token")}


async def test_grep_skips_oversized_files(tmp_path) -> None:
    (tmp_path / "big.txt").write_text("needle\n" + "x\n" * 1000, encoding="utf-8")
    (tmp_path / "small.txt").write_text("needle\n", encoding="utf-8")
    session = await _session(tmp_path, limits=WorkspaceLimits(max_grep_file_bytes=50))
    assert [m.path for m in await session.grep("needle")] == ["small.txt"]


async def test_grep_clips_long_lines(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("needle " + "z" * 1000 + "\n", encoding="utf-8")
    session = await _session(tmp_path, limits=WorkspaceLimits(max_grep_line_chars=20))
    matches = await session.grep("needle")
    assert len(matches) == 1 and len(matches[0].text) <= 20


async def test_grep_reports_nested_relative_paths(tmp_path) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "deep.txt").write_text("needle\n", encoding="utf-8")
    session = await _session(tmp_path)
    assert [m.path for m in await session.grep("needle")] == ["a/b/deep.txt"]


async def test_grep_skips_denied_directories(tmp_path) -> None:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "k.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("needle\n", encoding="utf-8")
    session = await _session(
        tmp_path, policy=WorkspacePolicy(denied_paths=("secrets",))
    )
    assert [m.path for m in await session.grep("needle")] == ["ok.txt"]


async def test_grep_glob_matches_by_path(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("needle\n", encoding="utf-8")
    session = await _session(tmp_path)
    only_src = await session.grep("needle", glob="src/*.py")
    assert [m.path for m in only_src] == ["src/app.py"]


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses file permissions",
)
async def test_grep_skips_unreadable_file(tmp_path) -> None:
    bad = tmp_path / "locked.txt"
    bad.write_text("needle\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("needle\n", encoding="utf-8")
    bad.chmod(0o000)
    try:
        session = await _session(tmp_path)
        # The unreadable file is skipped (logged at debug), not a crash.
        assert [m.path for m in await session.grep("needle")] == ["ok.txt"]
    finally:
        bad.chmod(0o644)


# ---------------------------------------------------------------------------
# concurrency & lock lifecycle
# ---------------------------------------------------------------------------


async def test_concurrent_edits_do_not_corrupt(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    session = await _session(tmp_path)
    # Each edit is an atomic read-modify-write under the per-path lock, so three
    # concurrent edits to distinct lines all land (no lost update).
    await asyncio.gather(
        session.edit_text("f.txt", "a", "A"),
        session.edit_text("f.txt", "b", "B"),
        session.edit_text("f.txt", "c", "C"),
    )
    assert (tmp_path / "f.txt").read_text() == "A\nB\nC\n"


async def test_concurrent_read_during_writes_is_never_torn(tmp_path) -> None:
    (tmp_path / "f.txt").write_text("A" * 5000, encoding="utf-8")
    session = await _session(tmp_path)
    valid = {"A" * 5000, "B" * 5000}

    async def writer() -> None:
        for i in range(40):
            await session.write_text("f.txt", ("B" if i % 2 else "A") * 5000)

    async def reader() -> None:
        for _ in range(40):
            # Read shares the write lock, so it sees a complete old-or-new file.
            assert (await session.read_text("f.txt")).content in valid

    await asyncio.gather(writer(), reader(), reader())


async def test_lock_map_evicts_after_use(tmp_path) -> None:
    session = await _session(tmp_path)
    for i in range(20):
        await session.write_text(f"f{i}.txt", "x")
    gc.collect()
    # Weak-valued map: no entry survives once its op is done — no unbounded growth.
    assert len(session._locks) == 0


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


async def test_run_in_relative_cwd(tmp_path) -> None:
    (tmp_path / "sub").mkdir()
    session = await _session(tmp_path)
    result = await session.run("pwd", cwd="sub")
    assert result.ok
    assert result.stdout.strip().endswith("/sub")


async def test_run_timeout_kills_process(tmp_path) -> None:
    session = await _session(tmp_path)
    result = await session.run("sleep 5", timeout=0.2)
    assert result.timed_out is True and result.exit_code is None


async def test_run_output_clipped_head_and_tail(tmp_path) -> None:
    session = await _session(
        tmp_path, limits=WorkspaceLimits(max_shell_output_chars=200)
    )
    result = await session.run("seq 1 2000")
    assert result.truncated is True
    assert "truncated" in result.stdout
    assert result.stdout.startswith("1\n")  # head kept
    assert result.stdout.rstrip().endswith("2000")  # tail kept


async def test_shell_env_excludes_host_secrets_by_default(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leaked-value-123")
    session = await _session(tmp_path)  # inherit_env defaults to False
    result = await session.run('echo "[$SUPER_SECRET_TOKEN]"')
    assert "leaked-value-123" not in result.stdout
    # ...but a working PATH is still present, so commands run.
    assert (await session.run("echo hi")).stdout.strip() == "hi"


async def test_shell_inherit_env_passes_host_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "leaked-value-123")
    session = await _session(tmp_path, inherit_env=True)
    result = await session.run('echo "[$SUPER_SECRET_TOKEN]"')
    assert "leaked-value-123" in result.stdout


async def test_shell_env_explicit_passthrough(tmp_path) -> None:
    session = await _session(tmp_path, env={"MY_VAR": "hello-env"})
    result = await session.run('echo "[$MY_VAR]"')
    assert "hello-env" in result.stdout


# ---------------------------------------------------------------------------
# Virtualenv auto-activation
# ---------------------------------------------------------------------------


def _make_venv(root, name: str = ".venv") -> None:
    """A minimal directory that passes the is-a-venv check (bin/python)."""
    (root / name / "bin").mkdir(parents=True)
    (root / name / "bin" / "python").touch()


async def test_shell_activates_root_venv(tmp_path) -> None:
    _make_venv(tmp_path)
    session = await _session(tmp_path)
    result = await session.run('echo "$PATH|$VIRTUAL_ENV"')
    path, _, venv = result.stdout.strip().partition("|")
    root = tmp_path.resolve()
    assert path.startswith(str(root / ".venv" / "bin") + os.pathsep)
    assert venv == str(root / ".venv")


async def test_venv_dot_form_preferred_over_plain(tmp_path) -> None:
    _make_venv(tmp_path, ".venv")
    _make_venv(tmp_path, "venv")
    session = await _session(tmp_path)
    result = await session.run('echo "$VIRTUAL_ENV"')
    assert result.stdout.strip().endswith("/.venv")


async def test_venv_plain_name_recognized(tmp_path) -> None:
    _make_venv(tmp_path, "venv")
    session = await _session(tmp_path)
    result = await session.run('echo "$VIRTUAL_ENV"')
    assert result.stdout.strip().endswith("/venv")


def test_venv_bin_dir_selects_windows_layout_on_windows(tmp_path, monkeypatch) -> None:
    # A unit test for the host-OS branch: on Windows the Scripts/python.exe
    # layout is the one that counts. (Faking os.name through a real session is
    # impossible — pathlib would switch Path to an uninstantiable WindowsPath —
    # but _venv_bin_dir never calls the Path factory, so it can be poked here.)
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").touch()
    monkeypatch.setattr(local_module.os, "name", "nt")
    found = local_module._venv_bin_dir(tmp_path)
    assert found is not None
    assert found[1] == tmp_path / ".venv" / "Scripts"


async def test_foreign_os_venv_layout_not_activated(tmp_path) -> None:
    # A venv carrying only the *other* OS's layout can't run here, so it must
    # not half-activate — VIRTUAL_ENV set around a bin dir python never enters.
    other = ("bin", "python") if os.name == "nt" else ("Scripts", "python.exe")
    (tmp_path / ".venv" / other[0]).mkdir(parents=True)
    (tmp_path / ".venv" / other[0] / other[1]).touch()
    session = await _session(tmp_path)
    result = await session.run('echo "[$VIRTUAL_ENV]"')
    assert result.stdout.strip() == "[]"


async def test_directory_merely_named_venv_is_not_activated(tmp_path) -> None:
    (tmp_path / ".venv").mkdir()  # no interpreter inside
    (tmp_path / "venv" / "bin").mkdir(parents=True)  # bin/ but no python
    session = await _session(tmp_path)
    result = await session.run('echo "[$VIRTUAL_ENV]"')
    assert result.stdout.strip() == "[]"


async def test_venv_created_mid_session_takes_effect(tmp_path) -> None:
    # The model's own flow: create the venv, then install into it — the very
    # next command must already resolve python/pip there (fresh process per
    # run; nothing to "keep activated").
    session = await _session(tmp_path)
    assert (await session.run('echo "[$VIRTUAL_ENV]"')).stdout.strip() == "[]"
    _make_venv(tmp_path)
    result = await session.run('echo "[$VIRTUAL_ENV]"')
    assert result.stdout.strip() == f"[{tmp_path.resolve() / '.venv'}]"


async def test_venv_yields_to_explicit_env_overrides(tmp_path) -> None:
    # env= merges after activation: an explicit PATH/VIRTUAL_ENV stays the
    # user's escape hatch (echo is a shell builtin, so the bogus PATH is fine).
    _make_venv(tmp_path)
    session = await _session(
        tmp_path, env={"PATH": "/custom-bin", "VIRTUAL_ENV": "/elsewhere"}
    )
    result = await session.run('echo "$PATH|$VIRTUAL_ENV"')
    assert result.stdout.strip() == "/custom-bin|/elsewhere"


async def test_venv_drops_inherited_pythonhome(tmp_path, monkeypatch) -> None:
    # activate's behavior: a lingering host PYTHONHOME would override the
    # venv's interpreter paths.
    monkeypatch.setenv("PYTHONHOME", "/somewhere")
    _make_venv(tmp_path)
    session = await _session(tmp_path, inherit_env=True)
    result = await session.run('echo "[$PYTHONHOME]"')
    assert result.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# Workspace config
# ---------------------------------------------------------------------------


async def test_workspace_local_mode_presets_and_overrides(tmp_path) -> None:
    ws = Workspace.local(str(tmp_path), mode="readonly")
    assert ws.policy.write == "deny"
    tool_names = [t.name for t in ws.tools()]
    assert tool_names == ["read_file", "list_files", "grep_files"]

    coding = Workspace.local(str(tmp_path), mode="coding")
    assert {t.name for t in coding.tools()} == {
        "read_file",
        "list_files",
        "grep_files",
        "write_file",
        "edit_file",
        "shell",
    }


async def test_workspace_local_rejects_conflicting_policy_kwargs(tmp_path) -> None:
    with pytest.raises(Exception, match="not both"):
        Workspace.local(
            str(tmp_path),
            policy=WorkspacePolicy.coding(),
            denied_paths=(".env",),
        )


async def test_workspace_instructions_reflect_policy(tmp_path) -> None:
    ws = Workspace.local(str(tmp_path), mode="readonly", denied_paths=(".env*",))
    text = ws.instructions()
    assert "read-only" in text
    assert ".env*" in text

    trusted = Workspace.local(str(tmp_path), mode="trusted")
    assert "without approval" in trusted.instructions()


async def test_workspace_instructions_venv_guidance_follows_shell(tmp_path) -> None:
    # The venv convention only matters where commands can run: present with a
    # shell, absent for readonly (which has none — nothing to install with).
    assert ".venv" in Workspace.local(str(tmp_path), mode="coding").instructions()
    assert ".venv" not in Workspace.local(str(tmp_path), mode="readonly").instructions()


async def test_workspace_instructions_default_to_pip_guidance(tmp_path) -> None:
    # No lockfile/marker: the plain pip + 'python -m venv' story, and no uv talk.
    text = Workspace.local(str(tmp_path), mode="coding").instructions()
    assert "python -m venv" in text
    assert "uv " not in text and "poetry" not in text


async def test_workspace_instructions_detect_uv(tmp_path) -> None:
    # A uv lockfile flips the guidance to uv's installers and warns off pip,
    # which uv venvs omit — the whole point of the flavor split.
    (tmp_path / "uv.lock").write_text("")
    text = Workspace.local(str(tmp_path), mode="coding").instructions()
    assert "uv pip install" in text
    assert "uv-managed" in text
    # The pip/stdlib-venv story that would dead-end here must be gone.
    assert "python -m venv" not in text
    assert "poetry" not in text


async def test_workspace_instructions_detect_uv_via_pyproject_marker(tmp_path) -> None:
    # No lockfile yet, but a [tool.uv] table is enough to know the flavor.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n\n[tool.uv]\ndev-dependencies = []\n"
    )
    text = Workspace.local(str(tmp_path), mode="coding").instructions()
    assert "uv pip install" in text
    assert "python -m venv" not in text


async def test_workspace_instructions_detect_poetry(tmp_path) -> None:
    (tmp_path / "poetry.lock").write_text("")
    text = Workspace.local(str(tmp_path), mode="coding").instructions()
    assert "poetry-managed" in text
    assert "poetry add" in text
    assert "python -m venv" not in text
    assert "uv pip install" not in text


async def test_workspace_inherit_env_defaults_off(tmp_path) -> None:
    # Every mode defaults to the minimal allowlist; inheriting the host env is
    # opt-in (even for trusted) so host secrets never leak unless asked for.
    assert Workspace.local(str(tmp_path), mode="trusted").inherit_env is False
    assert Workspace.local(str(tmp_path), mode="coding").inherit_env is False
    # ...but it is overridable either way.
    forced = Workspace.local(str(tmp_path), mode="coding", inherit_env=True)
    assert forced.inherit_env is True
    trusted_on = Workspace.local(str(tmp_path), mode="trusted", inherit_env=True)
    assert trusted_on.inherit_env is True


async def test_workspace_factory_guards() -> None:
    # Workspace is a factory facade, not a backend you instantiate.
    with pytest.raises(UserError):
        Workspace()
    # docker backend is a documented placeholder for now.
    with pytest.raises(NotImplementedError):
        Workspace.docker()


# ---------------------------------------------------------------------------
# Bootstrap / lifecycle edges
# ---------------------------------------------------------------------------


async def test_nonexistent_root_is_rejected(tmp_path) -> None:
    with pytest.raises(UserError, match="does not exist"):
        LocalWorkspaceSession(root=str(tmp_path / "ghost"))


async def test_async_context_manager_closes_on_exit(tmp_path) -> None:
    async with LocalWorkspaceSession(root=str(tmp_path)) as s:
        await s.list_files()
    with pytest.raises(WorkspaceClosedError):
        await s.read_text("anything")


# ---------------------------------------------------------------------------
# File-op argument guards
# ---------------------------------------------------------------------------


async def test_edit_text_on_missing_file_raises(tmp_path) -> None:
    session = await _session(tmp_path)
    with pytest.raises(WorkspaceError, match="Not a file"):
        await session.edit_text("ghost.txt", "a", "b")


async def test_list_files_on_a_file_raises(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    session = await _session(tmp_path)
    with pytest.raises(WorkspaceError, match="Not a directory"):
        await session.list_files("a.txt")


async def test_list_files_invalid_glob_pattern(tmp_path) -> None:
    session = await _session(tmp_path)
    with pytest.raises(WorkspaceError, match="Invalid glob"):
        await session.list_files(".", pattern=".")


# ---------------------------------------------------------------------------
# Listing / grep filters (hidden + policy-denied)
# ---------------------------------------------------------------------------


async def test_glob_skips_hidden_unless_requested(tmp_path) -> None:
    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    session = await _session(tmp_path)
    assert await session.list_files(".", pattern=".*", include_hidden=False) == []
    shown = await session.list_files(".", pattern=".*", include_hidden=True)
    assert any(e.path == ".env" for e in shown)


async def test_glob_skips_denied_paths(tmp_path) -> None:
    (tmp_path / "secret.txt").write_text("x", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("y", encoding="utf-8")
    session = await _session(
        tmp_path, policy=WorkspacePolicy(denied_paths=("secret.txt",))
    )
    paths = {e.path for e in await session.list_files(".", pattern="*")}
    assert "ok.txt" in paths
    assert "secret.txt" not in paths


async def test_glob_excludes_symlinks_escaping_root(tmp_path) -> None:
    # Security: a glob that traverses a symlink pointing outside the workspace
    # must not surface paths from outside the root.
    outside = tmp_path.parent / "escape_target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("leak", encoding="utf-8")
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)

    session = await _session(tmp_path)
    entries = await session.list_files(".", pattern="escape/*", include_hidden=True)
    assert entries == []


async def test_grep_skips_denied_files(tmp_path) -> None:
    (tmp_path / "secret.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("needle\n", encoding="utf-8")
    session = await _session(
        tmp_path, policy=WorkspacePolicy(denied_paths=("secret.txt",))
    )
    hits = await session.grep("needle")
    assert {m.path for m in hits} == {"ok.txt"}


# ---------------------------------------------------------------------------
# Shell cwd + env
# ---------------------------------------------------------------------------


async def test_run_rejects_non_directory_cwd(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    with pytest.raises(WorkspaceError, match="Not a directory"):
        await session.run("echo hi", cwd="ghostdir")


async def test_run_applies_env_override(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    result = await session.run('echo "$MYVAR"', env={"MYVAR": "from-test"})
    assert "from-test" in result.stdout


# ---------------------------------------------------------------------------
# Line-ending fidelity
# ---------------------------------------------------------------------------


async def test_read_preserves_crlf(tmp_path) -> None:
    (tmp_path / "f.txt").write_bytes(b"one\r\ntwo\r\n")
    session = await _session(tmp_path)
    result = await session.read_text("f.txt")
    # No universal-newline translation: the model sees what is on disk.
    assert result.content == "one\r\ntwo\r\n"
    assert result.total_lines == 2  # splitlines still counts CRLF lines


async def test_edit_preserves_crlf_line_endings(tmp_path) -> None:
    p = tmp_path / "f.txt"
    p.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    session = await _session(tmp_path)
    result = await session.edit_text("f.txt", "two", "2")
    assert result.ok
    # Only the edited span changed; the rest of the bytes are identical.
    assert p.read_bytes() == b"one\r\n2\r\nthree\r\n"


async def test_edit_upconverts_lf_span_for_crlf_file(tmp_path) -> None:
    # Models typically quote spans with plain \n; a CRLF file must still be
    # editable, and the replacement must stay CRLF so the file is consistent.
    p = tmp_path / "f.txt"
    p.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    session = await _session(tmp_path)
    result = await session.edit_text("f.txt", "alpha\nbeta", "ALPHA\nBETA")
    assert result.ok
    assert p.read_bytes() == b"ALPHA\r\nBETA\r\ngamma\r\n"


# ---------------------------------------------------------------------------
# Write durability
# ---------------------------------------------------------------------------


async def test_write_preserves_file_mode(tmp_path) -> None:
    p = tmp_path / "script.sh"
    p.write_text("#!/bin/sh\n", encoding="utf-8")
    p.chmod(0o755)
    session = await _session(tmp_path)
    await session.write_text("script.sh", "#!/bin/sh\necho hi\n")
    assert (p.stat().st_mode & 0o777) == 0o755
    await session.edit_text("script.sh", "echo hi", "echo bye")
    assert (p.stat().st_mode & 0o777) == 0o755


async def test_write_to_directory_path_raises(tmp_path) -> None:
    (tmp_path / "d").mkdir()
    session = await _session(tmp_path)
    with pytest.raises(WorkspaceError, match="directory"):
        await session.write_text("d", "content")


# ---------------------------------------------------------------------------
# grep on a single file
# ---------------------------------------------------------------------------


async def test_grep_accepts_a_file_path(tmp_path) -> None:
    (tmp_path / "app.py").write_text("needle = 1\nother\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("needle = 2\n", encoding="utf-8")
    session = await _session(tmp_path)
    matches = await session.grep("needle", path="app.py")
    assert [(m.path, m.line) for m in matches] == [("app.py", 1)]


async def test_grep_missing_path_raises(tmp_path) -> None:
    session = await _session(tmp_path)
    with pytest.raises(WorkspaceError, match="Not a file or directory"):
        await session.grep("x", path="ghost")


# ---------------------------------------------------------------------------
# Process lifecycle
# ---------------------------------------------------------------------------


async def test_cancelled_run_kills_the_process(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    marker = tmp_path / "marker"
    task = asyncio.create_task(session.run(f"sleep 1 && touch {marker}", timeout=30))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.2)
    # The child was killed with its process group; no orphan ran to completion.
    assert not marker.exists()


async def test_close_kills_inflight_processes(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    marker = tmp_path / "marker"
    task = asyncio.create_task(session.run(f"sleep 1 && touch {marker}", timeout=30))
    await asyncio.sleep(0.3)
    await session.close()
    result = await task  # the killed command surfaces as a failed result
    assert result.ok is False
    await asyncio.sleep(1.2)
    assert not marker.exists()


async def test_exit_code_reports_signal_death(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    result = await session.run("kill -9 $$")
    assert result.exit_code == -9
    assert result.ok is False


async def test_run_started_during_close_self_reaps(tmp_path) -> None:
    # Race: close() runs while run() is awaiting create_subprocess_shell, so
    # the child registers into an already-snapshotted set. run() must observe
    # _closed after registering and reap its own child rather than orphan it.
    session = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    marker = tmp_path / "marker"

    real_create = asyncio.create_subprocess_shell

    async def _closing_create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        await session.close()  # close() snapshots _procs before the add
        return proc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "create_subprocess_shell", _closing_create)
        with pytest.raises(WorkspaceClosedError):
            await session.run(f"sleep 1 && touch {marker}", timeout=30)
    await asyncio.sleep(1.2)
    assert not marker.exists()  # the child was killed, not orphaned


# ---------------------------------------------------------------------------
# Shell path guard (decide_command at the session)
# ---------------------------------------------------------------------------


async def test_shell_cannot_read_denied_paths(tmp_path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    session = await _session(
        tmp_path, policy=WorkspacePolicy.trusted(denied_paths=(".env*",))
    )
    assert session.decide_command("cat .env") == "deny"
    with pytest.raises(PermissionDeniedError):
        await session.run("cat .env")
    # Unrelated commands still run freely under trusted.
    assert (await session.run("echo ok")).ok


async def test_shell_redirect_to_outside_is_gated(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.coding())
    # write_outside="deny" -> a redirect target outside the root is denied.
    assert session.decide_command("echo x > ../evil.txt") == "deny"
    with pytest.raises(PermissionDeniedError):
        await session.run("echo x > ../evil.txt")
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_shell_outside_read_claims_escalate_to_ask(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    # trusted reads anywhere -> stays allow.
    assert session.decide_command("cat /etc/hosts") == "allow"
    coding = await _session(tmp_path, policy=WorkspacePolicy.coding())
    # coding asks for outside reads; the shell tool routes this to approval.
    assert coding.decide_command("cat /etc/hosts") == "ask"


async def test_listing_hides_symlink_to_denied_path(tmp_path) -> None:
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "alias.txt").symlink_to(tmp_path / ".env")
    session = await _session(tmp_path, policy=WorkspacePolicy(denied_paths=(".env*",)))
    listed = await session.list_files(".", include_hidden=True)
    assert "alias.txt" not in [e.path for e in listed]


async def test_grep_single_file_through_approved_symlink(tmp_path) -> None:
    # A single-file grep target was already gated as the operation's subject
    # (ask resolved at the tool layer), so the mid-walk symlink re-check must
    # not silently drop it.
    outside = tmp_path.parent / "shared_notes.txt"
    outside.write_text("needle here\n", encoding="utf-8")
    (tmp_path / "notes.txt").symlink_to(outside)
    session = await _session(tmp_path, policy=WorkspacePolicy.coding())
    matches = await session.grep("needle", path="notes.txt")
    assert len(matches) == 1


async def test_readonly_write_error_mentions_write_policy(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    session = await _session(tmp_path, policy=WorkspacePolicy.readonly())
    with pytest.raises(PermissionDeniedError, match="denies writing"):
        await session.write_text("a.txt", "y")


async def test_shell_cwd_is_part_of_the_decision(tmp_path) -> None:
    session = await _session(tmp_path, policy=WorkspacePolicy.trusted())
    # cwd resolving outside the root counts as an outside read claim.
    assert session.decide_command("ls", cwd="..") in ("allow", "ask")
    coding = await _session(tmp_path, policy=WorkspacePolicy.coding())
    assert coding.decide_command("ls", cwd="..") == "ask"


async def test_dev_null_plumbing_never_escalates(tmp_path) -> None:
    # `2>/dev/null` is ubiquitous; write_outside="deny" must not make an
    # allowed command fail because of it.
    session = await _session(
        tmp_path,
        policy=WorkspacePolicy.coding(command_rules=(CommandRule("pytest", "allow"),)),
    )
    assert session.decide_command("pytest -q 2>/dev/null") == "allow"
    assert session.decide_command("pytest -q > /dev/null 2>&1") == "allow"


async def test_writable_grant_allows_outside_writes(tmp_path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    session = LocalWorkspaceSession(
        root=str(root),
        policy=WorkspacePolicy(
            path_rules=(PathRule(out_dir.as_posix(), "allow"),),
        ),
    )
    result = await session.write_text(str(out_dir / "report.md"), "# out\n")
    assert result.action == "created"
    assert (out_dir / "report.md").read_text() == "# out\n"
    # The granted scope is readable too; unrelated outside paths stay denied.
    assert (await session.read_text(str(out_dir / "report.md"))).content == "# out\n"
    with pytest.raises(PermissionDeniedError):
        await session.write_text(str(tmp_path / "elsewhere.md"), "x")


async def test_glob_inside_explicit_dotdir_lists_entries(tmp_path) -> None:
    # Hidden filtering is relative to the listed directory: explicitly
    # listing ".config" must show its (non-hidden) contents.
    cfg = tmp_path / ".config"
    cfg.mkdir()
    (cfg / "settings.toml").write_text("x = 1\n", encoding="utf-8")
    (cfg / ".hidden.toml").write_text("h = 1\n", encoding="utf-8")
    session = await _session(tmp_path)
    entries = await session.list_files(".config", pattern="*")
    assert [e.path for e in entries] == [".config/settings.toml"]


async def test_edit_accepts_crlf_span_verbatim(tmp_path) -> None:
    # A model that echoes read_file content exactly supplies \r\n itself.
    p = tmp_path / "f.txt"
    p.write_bytes(b"a\r\nb\r\n")
    session = await _session(tmp_path)
    result = await session.edit_text("f.txt", "a\r\nb", "A\r\nB")
    assert result.ok
    assert p.read_bytes() == b"A\r\nB\r\n"


class _UppercaseExecutor:
    """Fake ShellExecutor: proves the seam is consulted and clipped."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, command, *, cwd, env, timeout, policy, root):
        from lovia.workspace.types import CommandResult

        self.calls.append(command)
        return CommandResult(exit_code=0, stdout="X" * 100, stderr="")


async def test_custom_executor_is_used_and_output_clipped(tmp_path) -> None:
    executor = _UppercaseExecutor()
    session = LocalWorkspaceSession(
        root=str(tmp_path),
        policy=WorkspacePolicy.trusted(),
        executor=executor,
        limits=WorkspaceLimits(max_shell_output_chars=40),
    )
    result = await session.run("echo hi")
    assert executor.calls == ["echo hi"]
    assert result.truncated is True
    assert len(result.stdout) < 100 + 60  # clipped with a notice, not raw
    # Policy still gates before the executor: a denied command never reaches it.
    denied = LocalWorkspaceSession(
        root=str(tmp_path),
        policy=WorkspacePolicy.trusted(command_rules=(CommandRule("rm", "deny"),)),
        executor=executor,
    )
    with pytest.raises(PermissionDeniedError):
        await denied.run("rm -rf .")
    assert executor.calls == ["echo hi"]
