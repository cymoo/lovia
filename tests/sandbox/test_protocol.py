"""Sandbox + SandboxProvider Protocol conformance."""

from __future__ import annotations

from pathlib import Path

from lovia.sandbox import (
    LocalSandbox,
    LocalSandboxProvider,
    Sandbox,
    SandboxProvider,
)


def test_local_sandbox_satisfies_protocol(tmp_path: Path) -> None:
    sb = LocalSandbox(root=tmp_path)
    assert isinstance(sb, Sandbox)


def test_local_provider_satisfies_protocol(tmp_path: Path) -> None:
    p = LocalSandboxProvider(root_base=tmp_path)
    assert isinstance(p, SandboxProvider)


def test_duck_typed_sandbox_satisfies_protocol() -> None:
    """A minimal ad-hoc impl should satisfy the Protocol via duck typing."""

    class Mem:
        id = "mem"
        workspace = "/workspace"

        async def read(self, path, *, max_bytes=None):
            return b""

        async def write(self, path, data, *, append=False):
            return 0

        async def ls(self, path=".", *, max_depth=1, include_hidden=False):
            return []

        async def glob(self, pattern, *, include_hidden=False):
            return []

        async def exists(self, path):
            return False

        async def remove(self, path, *, recursive=False):
            return None

        async def exec(self, cmd, *, cwd=None, env=None, stdin=None, limits=None):
            from lovia.sandbox import ExecResult

            return ExecResult(exit_code=0, stdout="", stderr="")

        async def close(self):
            return None

    assert isinstance(Mem(), Sandbox)
