"""Sandbox-backed ``edit_file`` tool factory."""

from __future__ import annotations

from typing import Annotated

from ..sandbox.protocol import SandboxSession
from ..sandbox.types import EditResult
from . import Tool, tool
from ._sandbox import sandbox_session


async def edit_exact(
    session: SandboxSession,
    path: str,
    old: str,
    new: str,
) -> EditResult:
    """Replace one exact text span in ``path``."""

    if old == "":
        return EditResult(
            ok=False,
            path=path,
            message="old must not be empty; read the file and provide an exact span",
        )
    current = await session.read_text(path)
    text = current.content
    if current.truncated:
        return EditResult(
            ok=False,
            path=current.path,
            message="file content was truncated; read a narrower range before editing",
        )
    count = text.count(old)
    if count == 0:
        return EditResult(
            ok=False,
            path=current.path,
            message="old text not found; read the file again and retry with exact text",
        )
    if count > 1:
        return EditResult(
            ok=False,
            path=current.path,
            replacements=count,
            message="old text matched multiple times; include more surrounding context",
        )
    if old == new:
        return EditResult(ok=True, path=current.path, replacements=1, changed=False)
    updated = text.replace(old, new, 1)
    await session.write_text(current.path, updated)
    return EditResult(ok=True, path=current.path, replacements=1, changed=True)


def edit_file(
    root: str | None = None,
    *,
    session: SandboxSession | None = None,
) -> Tool:
    """Create an ``edit_file`` tool."""

    sandbox = sandbox_session(root=root, session=session)

    @tool(
        name="edit_file",
        description=(
            "Replace exactly one occurrence of old text in a sandbox file. "
            "If no match or multiple matches are found, read the file and retry "
            "with a more precise old span."
        ),
    )
    async def _edit_file(
        path: Annotated[str, "Sandbox-relative file path."],
        old: Annotated[str, "Exact text to replace."],
        new: Annotated[str, "Replacement text."],
    ) -> object:
        return await edit_exact(sandbox, path, old, new)

    return _edit_file
