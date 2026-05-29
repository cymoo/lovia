"""Sandbox-backed ``glob`` tool factory."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..sandbox.protocol import SandboxSession
from . import Tool, tool
from ._sandbox import sandbox_session


def glob(
    root: str | None = None,
    *,
    session: SandboxSession | None = None,
) -> Tool:
    """Create a ``glob`` tool."""

    sandbox = sandbox_session(root=root, session=session)

    @tool(
        name="glob",
        description=(
            "Find sandbox paths matching a glob pattern. Hidden paths are "
            "skipped by default."
        ),
    )
    async def _glob(
        pattern: Annotated[str, "Glob pattern relative to the sandbox root."],
        include_hidden: Annotated[
            bool, Field(default=False, description="Include dotfiles/directories.")
        ] = False,
        max_results: Annotated[
            int, Field(default=1_000, ge=1, description="Maximum results.")
        ] = 1_000,
    ) -> list[str]:
        return await sandbox.glob(
            pattern, include_hidden=include_hidden, max_results=max_results
        )

    return _glob
