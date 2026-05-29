"""Sandbox-backed ``write_file`` tool factory."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..sandbox.protocol import SandboxSession
from . import Tool, tool
from ._sandbox import sandbox_session


def write_file(
    root: str | None = None,
    *,
    session: SandboxSession | None = None,
) -> Tool:
    """Create a ``write_file`` tool."""

    sandbox = sandbox_session(root=root, session=session)

    @tool(
        name="write_file",
        description=(
            "Write a UTF-8 file inside the sandbox. Prefer edit_file for "
            "targeted changes; use write_file for new files or full rewrites."
        ),
    )
    async def _write_file(
        path: Annotated[str, "Sandbox-relative file path."],
        content: Annotated[str, "Full file content to write."],
        create_only: Annotated[
            bool,
            Field(
                default=False, description="If true, do not overwrite an existing file."
            ),
        ] = False,
    ) -> object:
        return await sandbox.write_text(path, content, create_only=create_only)

    return _write_file
