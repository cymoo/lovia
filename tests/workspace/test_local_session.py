"""LocalWorkspaceSession behavior: path safety, files, search, shell."""

from __future__ import annotations

import asyncio
import gc
import os

import pytest

from lovia.exceptions import UserError
from lovia.workspace import (
    CommandRule,
    LocalWorkspaceSession,
    PathOutsideWorkspaceError,
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
# Path safety
# ---------------------------------------------------------------------------


async def test_rejects_absolute_and_escape_paths(tmp_path) -> None:
    session = await _session(tmp_path)
    with pytest.raises(PathOutsideWorkspaceError):
        await session.read_text("/etc/passwd")
    with pytest.raises(PathOutsideWorkspaceError):
        await session.read_text("../outside.txt")
    with pytest.raises(PathOutsideWorkspaceError):
        await session.write_text("a/../../escape.txt", "x")


async def test_rejects_symlink_escape(tmp_path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    session = await _session(tmp_path)
    with pytest.raises(PathOutsideWorkspaceError):
        await session.read_text("link.txt")


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
    session = await _session(
        tmp_path, limits=WorkspaceLimits(max_file_read_bytes=200)
    )
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
    with pytest.raises(PathOutsideWorkspaceError):
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
    session = await _session(tmp_path, policy=WorkspacePolicy(denied_paths=("secrets",)))
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


async def test_shell_env_excludes_host_secrets_by_default(tmp_path, monkeypatch) -> None:
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
# Workspace config
# ---------------------------------------------------------------------------


async def test_workspace_local_mode_presets_and_overrides(tmp_path) -> None:
    ws = Workspace.local(str(tmp_path), mode="readonly")
    assert not ws.policy.allow_write
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
    session = await _session(tmp_path, policy=WorkspacePolicy(denied_paths=("secret.txt",)))
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
    session = await _session(tmp_path, policy=WorkspacePolicy(denied_paths=("secret.txt",)))
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
