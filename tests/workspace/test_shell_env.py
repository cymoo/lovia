"""Shell environment regressions, runnable on Windows without external services."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from types import SimpleNamespace

import pytest

from lovia.workspace import LocalWorkspaceSession, Workspace
from lovia.workspace import local as local_module


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_base_env_keeps_platform_basics_only(tmp_path, monkeypatch, platform):
    host = {
        "PATH": "bin",
        "HOME": "home",
        "LC_ALL": "C",
        "SystemRoot": "windows",
        "windir": "windows",
        "ComSpec": "cmd.exe",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "TEMP": "temp",
        "TMP": "tmp",
        "OPENAI_API_KEY": "secret",
        "CUSTOM_TOKEN": "secret",
        "HTTPS_PROXY": "proxy",
        "SSL_CERT_FILE": "ca.pem",
        "USERPROFILE": "profile",
        "APPDATA": "appdata",
        "LOCALAPPDATA": "localappdata",
    }
    session = LocalWorkspaceSession(root=tmp_path)
    # Replace only this module's os reference: changing os.name globally
    # would also switch pathlib/pytest to the other platform's path class.
    monkeypatch.setattr(
        local_module, "os", SimpleNamespace(name=platform, environ=host)
    )
    expected = {key: host[key] for key in ("PATH", "HOME", "LC_ALL")}
    if platform == "nt":
        expected.update(
            {
                key.upper(): host[key]
                for key in (
                    "SystemRoot",
                    "windir",
                    "ComSpec",
                    "PATHEXT",
                    "TEMP",
                    "TMP",
                    "USERPROFILE",
                    "APPDATA",
                    "LOCALAPPDATA",
                )
            }
        )
    assert session._base_env() == expected


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_base_env_does_not_invent_missing_variables(tmp_path, monkeypatch, platform):
    session = LocalWorkspaceSession(root=tmp_path)
    monkeypatch.setattr(local_module, "os", SimpleNamespace(name=platform, environ={}))
    assert session._base_env() == {}


@pytest.mark.parametrize("background", [False, True], ids=["run", "spawn"])
@pytest.mark.parametrize("inherit_env", [False, True], ids=["minimal", "inherited"])
async def test_shell_socket_and_environment(
    tmp_path, monkeypatch, background, inherit_env
):
    """Exercise the real child environment; Winsock must load on Windows."""
    monkeypatch.setenv("LOVIA_TEST_SECRET", "host-secret")
    monkeypatch.setenv("LOVIA_TEST_OVERRIDE", "host")
    (tmp_path / "probe.py").write_text(
        "import getpass, json, os, pathlib, socket, tempfile\n"
        "with socket.socket():\n"
        "    pass\n"
        "with tempfile.TemporaryFile() as f:\n"
        "    f.write(b'ok')\n"
        "pathlib.Path.home()\n"
        "getpass.getuser()\n"
        "print(json.dumps({\n"
        "    'secret': os.getenv('LOVIA_TEST_SECRET'),\n"
        "    'override': os.getenv('LOVIA_TEST_OVERRIDE'),\n"
        "    'workspace': os.getenv('LOVIA_TEST_WORKSPACE'),\n"
        "    'systemroot': os.getenv('SYSTEMROOT'),\n"
        "}))\n",
        encoding="utf-8",
    )
    workspace = Workspace.local(
        str(tmp_path),
        env={"LOVIA_TEST_OVERRIDE": "workspace", "LOVIA_TEST_WORKSPACE": "kept"},
        inherit_env=inherit_env,
    )
    session = await workspace.open()
    # cmd.exe and POSIX shells use different quoting rules. Invoke this
    # interpreter explicitly so a different Python on PATH cannot hide a bug.
    python = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    command = f"{python} probe.py"
    overrides = {"LOVIA_TEST_OVERRIDE": "command"}
    try:
        if background:
            started = await session.spawn(command, env=overrides)

            async def collect():
                output = ""
                while True:
                    result = await session.read_process_output(started.process_id)
                    output += result.output
                    if result.exit_code is not None:
                        return result.exit_code, output
                    await asyncio.sleep(0.02)

            exit_code, output = await asyncio.wait_for(collect(), timeout=20)
        else:
            result = await session.run(command, env=overrides, timeout=20)
            exit_code, output = result.exit_code, result.stdout
            assert not result.stderr
        assert exit_code == 0, output
        payload = json.loads(output)
        assert payload["secret"] == ("host-secret" if inherit_env else None)
        assert payload["override"] == "command"
        assert payload["workspace"] == "kept"
        if os.name == "nt":
            assert payload["systemroot"] == os.environ["SYSTEMROOT"]
    finally:
        await session.close()


def test_windows_cleanup_kills_tree_before_shell(monkeypatch):
    calls = []
    # killpg/getpgid do not exist on Windows, even for an exited process.
    monkeypatch.setattr(local_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        local_module.subprocess, "run", lambda args, **_: calls.append(args)
    )
    proc = SimpleNamespace(pid=42, kill=lambda: calls.append("kill"))
    local_module._kill_process_group(proc)
    assert calls == [["taskkill", "/F", "/T", "/PID", "42"], "kill"]


@pytest.mark.parametrize("background", [False, True], ids=["run", "spawn"])
async def test_kill_reaps_grandchild_holding_pipes(tmp_path, background):
    """The shell's child inherits its pipes: killing only the shell would
    orphan it, and leave run()'s wait() blocked until it exits on its own."""
    beat = tmp_path / "beat"
    (tmp_path / "beater.py").write_text(
        "import time\n"
        "while True:\n"
        "    with open('beat', 'a') as f:\n"
        "        f.write('.')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    python = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
    # `&& echo` keeps a POSIX shell from exec'ing python in its place.
    command = f"{python} beater.py && echo done"
    session = await Workspace.local(str(tmp_path)).open()
    try:
        if background:
            started = await session.spawn(command)
            while not beat.exists():
                await asyncio.sleep(0.05)
            await asyncio.wait_for(session.kill_process(started.process_id), 20)
        else:
            result = await asyncio.wait_for(session.run(command, timeout=2), 20)
            assert result.timed_out
        size = beat.stat().st_size
        await asyncio.sleep(0.5)
        assert beat.stat().st_size == size, "descendant still running"
    finally:
        await asyncio.wait_for(session.close(), 20)
