"""Bring-your-own workspace backend.

Implement :class:`WorkspaceBackend` to plug Docker, Firecracker, a remote
runner, or an in-memory fake into the same Tool factories.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from lovia.workspace import (
    DirEntry,
    ExecLimits,
    ExecResult,
    Workspace,
    WorkspaceBackend,
    read_file,
    write_file,
)


@dataclass
class MemoryWorkspace:
    id: str = "memory"
    workspace: str = "/workspace"
    files: dict[str, bytes] = field(default_factory=dict)

    async def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        return self.files[path]

    async def write(
        self,
        path: str,
        data: bytes | str,
        *,
        append: bool = False,
        overwrite: bool = True,
    ) -> int:
        payload = data.encode() if isinstance(data, str) else data
        self.files[path] = (self.files.get(path, b"") if append else b"") + payload
        return len(payload)

    async def edit(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
    ) -> int:
        text = self.files[path].decode()
        self.files[path] = text.replace(old_text, new_text).encode()
        return text.count(old_text)

    async def list_dir(
        self,
        path: str = ".",
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[DirEntry]:
        return [
            DirEntry(name=name, is_dir=False, size=len(data))
            for name, data in self.files.items()
        ]

    async def glob(
        self,
        pattern: str,
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[str]:
        return sorted(self.files)

    async def exists(self, path: str) -> bool:
        return path in self.files

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        self.files.pop(path, None)

    async def exec(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | bytes | None = None,
        limits: ExecLimits | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=0, stdout=f"would run: {command}", stderr="")

    async def close(self) -> None:
        pass


async def main() -> None:
    backend: WorkspaceBackend = MemoryWorkspace()
    ws = Workspace(backend=backend)
    await ws.write_file("hello.txt", "hello")
    assert await ws.read_file("hello.txt") == "hello"
    assert write_file(ws).name == "write_file"
    assert read_file(ws).name == "read_file"
    print("custom backend works")


if __name__ == "__main__":
    asyncio.run(main())
