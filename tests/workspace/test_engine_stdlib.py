"""The workspace session engine runs on the standard library alone.

The files in :data:`~lovia.workspace.remote.bundle.ENGINE` are what a remote
workspace ships to the far side and runs there, where lovia's dependencies
are not installed (``tests/workspace/test_remote.py`` does exactly that,
through the real bootstrap). This copies
them into a bare package tree and drives a real session in a subprocess that
has no site-packages at all (``python -I -S``), so an import that reaches
outside the standard library — top-level or deferred into a function — fails
here rather than on someone's server.

Adding a file the engine needs? Add it to :data:`ENGINE`. Adding a
third-party import to one of them? Don't; see AGENTS.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import lovia
from lovia.workspace.remote.bundle import ENGINE

# Runs in the bare interpreter. Exercises every kind of operation, since a
# deferred import only fails when its code path runs.
_DRIVER = r"""
import asyncio, dataclasses, json, sys

from lovia.workspace.errors import PermissionDeniedError
from lovia.workspace.local import LocalWorkspaceSession
from lovia.workspace.policy import WorkspacePolicy
from lovia.workspace.remote import serve, wire  # the far side's own two files


async def main(root):
    s = LocalWorkspaceSession(root=root, policy=WorkspacePolicy.trusted())
    out = {}
    out["write"] = (await s.write_text("a/b.txt", "one\ntwo\n")).action
    out["bytes"] = (await s.write_bytes("a/c.bin", bytes(range(256)))).bytes_written
    out["read"] = (await s.read_text("a/b.txt")).content
    out["read_bytes"] = len((await s.read_bytes("a/c.bin")).data)
    out["edit"] = (await s.edit_text("a/b.txt", "two", "2")).replacements
    out["list"] = sorted(e.path for e in await s.list_files(".", pattern="**/*"))
    out["grep"] = [m.line for m in await s.grep("one")]
    out["decide"] = s.decide_command("git push --force").action
    run = await s.run("printf '%0200d' 7", timeout=20)
    out["run"] = dataclasses.asdict(run)
    started = await s.spawn("sleep 30")
    out["bg"] = [p.status for p in s.background_processes()]
    out["killed"] = (await s.kill_process(started.process_id)).status
    await s.close()

    readonly = LocalWorkspaceSession(root=root, policy=WorkspacePolicy.readonly())
    try:
        await readonly.write_text("x.txt", "x")
        out["readonly"] = "wrote"
    except PermissionDeniedError as exc:
        out["readonly"] = "denied"
    await readonly.close()

    names = {name.split(".")[0] for name in sys.modules}
    out["foreign"] = sorted(
        n for n in names
        if n not in sys.stdlib_module_names and n not in ("lovia", "__main__")
    )
    print(json.dumps(out))


asyncio.run(main(sys.argv[1]))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="drives a POSIX shell")
def test_engine_runs_without_third_party_packages(tmp_path) -> None:
    src = Path(lovia.__file__).parent
    pkg = tmp_path / "pkg" / "lovia"
    for rel in ENGINE:
        target = pkg / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((src / rel).read_text("utf-8"), encoding="utf-8")
    for directory in (pkg, *(d for d in pkg.rglob("*") if d.is_dir())):
        (directory / "__init__.py").write_text("", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()

    driver = f"import sys; sys.path.insert(0, {str(pkg.parent)!r})\n" + _DRIVER
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", driver, str(root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    assert out["foreign"] == []
    assert out["write"] == "created" and out["bytes"] == 256
    assert out["read"] == "one\ntwo\n" and out["read_bytes"] == 256
    assert out["edit"] == 1
    assert out["list"] == ["a", "a/b.txt", "a/c.bin"]
    assert out["grep"] == [1]
    assert out["decide"] == "deny"
    assert out["run"]["exit_code"] == 0 and len(out["run"]["stdout"]) == 200
    assert out["bg"] == ["running"] and out["killed"] == "killed"
    assert out["readonly"] == "denied"
