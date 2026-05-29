"""Sandbox-backed ``list_dir`` tool factory."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..sandbox.protocol import SandboxSession
from . import Tool, tool
from ._sandbox import sandbox_session


def list_dir(
    root: str | None = None,
    *,
    session: SandboxSession | None = None,
) -> Tool:
    """Create a ``list_dir`` tool."""

    sandbox = sandbox_session(root=root, session=session)

    @tool(
        name="list_dir",
        description="List direct children of a sandbox directory.",
    )
    async def _list_dir(
        path: Annotated[str, "Sandbox-relative directory path."] = ".",
        include_hidden: Annotated[
            bool, Field(default=False, description="Include dotfiles/directories.")
        ] = False,
        max_results: Annotated[
            int, Field(default=1_000, ge=1, description="Maximum entries.")
        ] = 1_000,
    ) -> object:
        return await sandbox.list_dir(
            path, include_hidden=include_hidden, max_results=max_results
        )

    return _list_dir
