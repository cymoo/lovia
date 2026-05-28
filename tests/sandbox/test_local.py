"""LocalSandbox: filesystem + exec semantics."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from lovia.sandbox import ExecLimits, LocalSandbox, SandboxClosed
from lovia.sandbox.errors import PathEscape


async def test_read_write_roundtrip(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    n = await sb.write("hello.txt", "hi")
    assert n == 2
    assert (await sb.read("hello.txt")) == b"hi"


async def test_write_with_absolute_workspace_path(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    await sb.write("/workspace/app.py", "x=1")
    assert (tmp_path / "app.py").read_text() == "x=1"


async def test_write_traversal_blocked(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    with pytest.raises(PathEscape):
        await sb.write("../escape.txt", "no")


async def test_append_mode(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    await sb.write("log.txt", "a")
    await sb.write("log.txt", "b", append=True)
    assert (await sb.read("log.txt")) == b"ab"


# ---- listing & hidden files -------------------------------------------------


async def test_ls_returns_dir_entries(seeded_root: Path) -> None:
    sb = LocalSandbox(root=seeded_root)
    entries = await sb.ls(".")
    names = {e.name for e in entries}
    # .lovia/ is created by __post_init__ but hidden by default.
    assert names == {"a.txt", "sub"}
    a = next(e for e in entries if e.name == "a.txt")
    assert a.size == 5
    assert a.is_dir is False


async def test_ls_hides_dotfiles_by_default(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    await sb.write(".env", "SECRET=1")
    await sb.write("visible.txt", "x")
    names = {e.name for e in await sb.ls(".")}
    assert ".env" not in names
    assert ".lovia" not in names
    assert "visible.txt" in names


async def test_ls_include_hidden(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    await sb.write(".env", "x")
    names = {e.name for e in await sb.ls(".", include_hidden=True)}
    assert ".env" in names
    assert ".lovia" in names  # framework bookkeeping is visible when asked


async def test_glob(seeded_root: Path) -> None:
    sb = LocalSandbox(root=seeded_root)
    assert (await sb.glob("**/*.py")) == ["sub/b.py"]


async def test_glob_skips_hidden_dirs(tmp_path: Path) -> None:
    """The LLM's own .venv shouldn't drown out ``**/*.py``."""
    sb = LocalSandbox(root=tmp_path)
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "junk.py").write_text("x")
    await sb.write("app.py", "y")
    assert (await sb.glob("**/*.py")) == ["app.py"]
    hidden = await sb.glob("**/*.py", include_hidden=True)
    assert ".venv/lib/junk.py" in hidden


# ---- removal ---------------------------------------------------------------


async def test_exists_and_remove(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    await sb.write("x.txt", "z")
    assert await sb.exists("x.txt") is True
    await sb.remove("x.txt")
    assert await sb.exists("x.txt") is False


async def test_remove_recursive(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_text("x")
    with pytest.raises(Exception):
        await sb.remove("d", recursive=False)
    await sb.remove("d", recursive=True)
    assert not (tmp_path / "d").exists()


# ---- exec ------------------------------------------------------------------


async def test_exec_basic(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


async def test_exec_argv(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec([sys.executable, "-c", "print(2+2)"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "4"


async def test_exec_timeout(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec("sleep 2", limits=ExecLimits(timeout=0.3))
    assert result.timed_out is True
    assert result.exit_code != 0


async def test_exec_output_truncation(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec(
        f"{sys.executable} -c 'print(\"X\"*10000)'",
        limits=ExecLimits(max_output_bytes=100),
    )
    assert result.truncated is True
    assert len(result.stdout) <= 200


async def test_exec_stdin(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec("cat", stdin="piped\n")
    assert "piped" in result.stdout


async def test_exec_cwd(seeded_root: Path) -> None:
    sb = LocalSandbox(root=seeded_root)
    result = await sb.exec("pwd", cwd="sub")
    assert result.stdout.strip().endswith("/sub")


# ---- environment isolation -------------------------------------------------


async def test_exec_redirects_home(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec("echo $HOME")
    assert result.stdout.strip() == str((tmp_path / ".lovia" / "home").resolve())


async def test_exec_redirects_tmpdir(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec("echo $TMPDIR")
    assert result.stdout.strip() == str((tmp_path / ".lovia" / "tmp").resolve())


async def test_exec_prepends_venv_to_path(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec("echo $PATH")
    expected = str((tmp_path / ".venv" / "bin").resolve())
    assert result.stdout.strip().startswith(expected + os.pathsep)


async def test_exec_overrides_pip_cache(tmp_path: Path) -> None:
    """A bare ``pip install --user`` shouldn't touch the host's ~/.cache."""
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec("echo $PIP_CACHE_DIR")
    assert str(tmp_path / ".lovia" / "home") in result.stdout


async def test_caller_env_wins_over_lovia_overrides(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    result = await sb.exec("echo $HOME", env={"HOME": "/explicit"})
    assert result.stdout.strip() == "/explicit"


async def test_exec_env_merges_with_self_env(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path, env={"FOO": "1"})
    result = await sb.exec("echo $FOO-$BAR", env={"BAR": "2"})
    assert "1-2" in result.stdout


async def test_venv_bootstrap_isolates_python(tmp_path: Path) -> None:
    """End-to-end: the LLM's `python -m venv .venv` is picked up next call.

    No special API: just the same PATH prefix already there.
    """
    sb = LocalSandbox(root=tmp_path)
    # Create a venv. Use the host Python explicitly so we don't depend
    # on the host's `python` being on PATH (it might not be).
    boot = await sb.exec(f"{sys.executable} -m venv .venv")
    assert boot.exit_code == 0, boot.stderr

    # Next exec: `python` should now resolve to the venv.
    which = await sb.exec("which python")
    assert which.exit_code == 0
    assert str(tmp_path / ".venv" / "bin" / "python") in which.stdout


# ---- lifecycle -------------------------------------------------------------


async def test_close_idempotent(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    await sb.close()
    await sb.close()
    with pytest.raises(SandboxClosed):
        await sb.read(".")


async def test_ephemeral_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "eph"
    root.mkdir()
    sb = LocalSandbox(root=root, ephemeral=True)
    await sb.write("a", "1")
    await sb.close()
    assert not root.exists()


async def test_async_context_manager(tmp_path: Path) -> None:
    async with LocalSandbox(root=tmp_path) as sb:
        await sb.write("x", "1")
    assert sb._closed is True  # type: ignore[attr-defined]


async def test_max_bytes_read_enforced(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    await sb.write("big.txt", "a" * 100)
    with pytest.raises(Exception):
        await sb.read("big.txt", max_bytes=10)
