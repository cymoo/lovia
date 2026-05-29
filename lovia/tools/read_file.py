"""Sandbox-backed ``read_file`` tool factory."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..sandbox.protocol import SandboxSession
from . import Tool, tool
from ._sandbox import sandbox_session


def read_file(
    root: str | None = None,
    *,
    session: SandboxSession | None = None,
) -> Tool:
    """Create a ``read_file`` tool."""

    sandbox = sandbox_session(root=root, session=session)

    @tool(
        name="read_file",
        description=(
            "Read a UTF-8 text file from the sandbox. Paths must be relative to "
            "the sandbox root. Use start/end line numbers for large files."
        ),
    )
    async def _read_file(
        path: Annotated[str, "Sandbox-relative file path."],
        start: Annotated[
            int | None,
            Field(default=None, ge=1, description="1-based start line."),
        ] = None,
        end: Annotated[
            int | None,
            Field(default=None, ge=1, description="1-based inclusive end line."),
        ] = None,
    ) -> object:
        return await sandbox.read_text(path, start=start, end=end)

    return _read_file
