"""Bring-your-own sandbox: implement the :class:`Sandbox` Protocol.

This example sketches an in-memory sandbox to demonstrate that the
sandbox layer has no inheritance requirement — any object that quacks
right satisfies the Protocol. The same shape is what a real Docker /
Firecracker / Kubernetes adapter would implement.

Run::

    python examples/24_custom_sandbox.py
"""

from __future__ import annotations

import os
import asyncio
import fnmatch
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from lovia import (
    Agent,
    DirEntry,
    ExecLimits,
    ExecResult,
    Runner,
    Sandbox,
    sandbox_tools,
)

MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-4o-mini")


@dataclass
class InMemorySandbox:
    """Tiny in-memory ``Sandbox`` implementation. Not isolated, not safe
    for untrusted code — purely an illustration."""

    id: str = "mem"
    workspace: str = "/workspace"
    _files: dict[str, bytes] = field(default_factory=dict)
    _closed: bool = False

    async def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        return self._files[path.lstrip("/").removeprefix("workspace/")]

    async def write(self, path: str, data: bytes | str, *, append: bool = False) -> int:
        key = path.lstrip("/").removeprefix("workspace/")
        body = data.encode() if isinstance(data, str) else data
        if append and key in self._files:
            body = self._files[key] + body
        self._files[key] = body
        return len(body)

    async def ls(
        self,
        path: str = ".",
        *,
        max_depth: int = 1,
        include_hidden: bool = False,
    ) -> list[DirEntry]:
        return [
            DirEntry(name=k, is_dir=False, size=len(v))
            for k, v in self._files.items()
            if include_hidden or not k.startswith(".")
        ]

    async def glob(self, pattern: str, *, include_hidden: bool = False) -> list[str]:
        return [
            k
            for k in self._files
            if fnmatch.fnmatch(k, pattern) and (include_hidden or not k.startswith("."))
        ]

    async def exists(self, path: str) -> bool:
        return path.lstrip("/").removeprefix("workspace/") in self._files

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        self._files.pop(path.lstrip("/").removeprefix("workspace/"), None)

    async def exec(
        self,
        cmd: "str | Sequence[str]",
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: "str | bytes | None" = None,
        limits: ExecLimits | None = None,
    ) -> ExecResult:
        return ExecResult(exit_code=0, stdout=f"(mem) ran: {cmd}\n", stderr="")

    async def close(self) -> None:
        self._closed = True


async def main() -> None:
    sb = InMemorySandbox()
    assert isinstance(sb, Sandbox)  # Protocol conformance — runtime checked.

    agent = Agent(
        name="memdemo",
        instructions="Write a file, list it, run a fake command.",
        model=MODEL,
        tools=sandbox_tools(sb),
    )
    # We do not actually call the LLM here; just demonstrate wiring.
    print(f"sandbox id: {sb.id}")
    print(f"tools attached: {[t.name for t in agent.tools]}")
    await sb.write("greet.txt", "hello")
    print(f"files: {[e.name for e in await sb.ls()]}")
    _ = Runner  # silence unused-import warning


if __name__ == "__main__":
    asyncio.run(main())
