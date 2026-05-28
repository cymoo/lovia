"""Sandboxed filesystem tools rooted at a single directory.

All paths are resolved and validated to live under ``root``; any attempt to
escape (via ``..`` or symlinks) raises :class:`~lovia.exceptions.ToolError`.

::

    from lovia.builtins.fs import FileSystem
    fs = FileSystem(root="/work", writable=True)
    agent = Agent(name="x", tools=fs.tools())
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from ..exceptions import ToolError, UserError
from ..tools import Tool, tool


@dataclass
class FileSystem:
    """A bag of sandboxed file tools.

    Construct once, then attach ``fs.tools()`` to your agent.

    Args:
        root: Directory all operations are confined to.
        writable: When ``False`` (default) ``write_file`` is omitted.
        max_bytes: Maximum payload size for read/write, in bytes.
    """

    root: str | Path
    writable: bool = False
    max_bytes: int = 1_000_000
    _root: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._root = Path(self.root).expanduser().resolve()
        if not self._root.is_dir():
            raise UserError(
                f"FileSystem root does not exist: {self._root}",
                hint="Create the directory first or point to an existing one.",
            )

    def _resolve(self, relpath: str) -> Path:
        target = (self._root / relpath).expanduser().resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ToolError(
                f"Path {relpath!r} escapes the sandbox root.",
                hint=f"All paths must be inside {self._root}.",
            ) from exc
        return target

    def tools(self) -> list[Tool]:
        """Return the configured tool set."""
        root = self._root
        max_bytes = self.max_bytes
        resolve = self._resolve

        @tool
        def read_file(
            path: Annotated[str, "Relative path under the sandbox root."],
        ) -> str:
            """Read a UTF-8 text file inside the sandbox."""
            p = resolve(path)
            if not p.is_file():
                raise ToolError(f"Not a file: {path}")
            data = p.read_bytes()
            if len(data) > max_bytes:
                raise ToolError(
                    f"File too large ({len(data)} > {max_bytes} bytes).",
                    hint="Increase FileSystem(max_bytes=...) or read a slice externally.",
                )
            return data.decode("utf-8", errors="replace")

        @tool
        def list_dir(
            path: Annotated[str, "Relative directory path, '.' for root."] = ".",
        ) -> list[str]:
            """List entries in a directory (names only)."""
            p = resolve(path)
            if not p.is_dir():
                raise ToolError(f"Not a directory: {path}")
            return sorted(os.listdir(p))

        @tool
        def glob(
            pattern: Annotated[str, "Glob pattern, e.g. '**/*.py'."],
        ) -> list[str]:
            """Return paths matching ``pattern`` (relative to the sandbox root)."""
            return sorted(str(p.relative_to(root)) for p in root.glob(pattern))

        tools_list: list[Tool] = [read_file, list_dir, glob]

        if self.writable:

            @tool
            def write_file(
                path: Annotated[str, "Relative path under the sandbox root."],
                content: Annotated[str, "UTF-8 text to write (overwrites)."],
            ) -> str:
                """Write a UTF-8 text file inside the sandbox."""
                p = resolve(path)
                payload = content.encode("utf-8")
                if len(payload) > max_bytes:
                    raise ToolError(
                        f"Payload too large ({len(payload)} > {max_bytes} bytes).",
                    )
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(payload)
                return f"wrote {len(payload)} bytes to {path}"

            tools_list.append(write_file)

        return tools_list
