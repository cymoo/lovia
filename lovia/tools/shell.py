"""Sandbox-backed ``shell`` tool factory."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..sandbox.protocol import SandboxSession
from . import Tool, tool
from ._sandbox import sandbox_session


def shell(
    root: str | None = None,
    *,
    session: SandboxSession | None = None,
    needs_approval: bool = False,
) -> Tool:
    """Create a one-shot ``shell`` tool."""

    sandbox = sandbox_session(root=root, session=session)

    @tool(
        name="shell",
        description=(
            "Run a one-shot non-interactive shell command in the sandbox. "
            "cwd must be relative to the sandbox root. Local sandboxes are not "
            "a hard security boundary; approved commands run as the host user."
        ),
        needs_approval=needs_approval,
    )
    async def _shell(
        command: Annotated[str, "Shell command to run."],
        cwd: Annotated[str, "Sandbox-relative working directory."] = ".",
        timeout: Annotated[
            float | None,
            Field(default=None, description="Override timeout in seconds."),
        ] = None,
        reason: Annotated[
            str | None,
            Field(default=None, description="Optional reason shown in approval UI."),
        ] = None,
    ) -> object:
        _ = reason
        return await sandbox.run(command, cwd=cwd, timeout=timeout)

    return _shell
