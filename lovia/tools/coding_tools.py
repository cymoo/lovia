"""Standard sandbox-backed coding tool bundle."""

from __future__ import annotations

from ..sandbox.errors import SandboxError
from ..sandbox.protocol import SandboxSession
from ..sandbox.types import SandboxMode
from . import Tool
from ._sandbox import sandbox_session
from .edit_file import edit_file
from .glob import glob
from .list_dir import list_dir
from .read_file import read_file
from .shell import shell
from .write_file import write_file


def coding_tools(
    root: str | None = None,
    *,
    session: SandboxSession | None = None,
    mode: SandboxMode = "coding",
) -> list[Tool]:
    """Return the standard coding tools bound to ``root`` or ``session``."""

    sandbox = sandbox_session(root=root, session=session)
    if mode == "readonly":
        return [
            read_file(session=sandbox),
            list_dir(session=sandbox),
            glob(session=sandbox),
        ]
    if mode == "trusted":
        shell_needs_approval = False
    elif mode == "coding":
        shell_needs_approval = True
    else:
        raise SandboxError(f"Unknown sandbox mode: {mode!r}")
    return [
        read_file(session=sandbox),
        write_file(session=sandbox),
        edit_file(session=sandbox),
        list_dir(session=sandbox),
        glob(session=sandbox),
        shell(session=sandbox, needs_approval=shell_needs_approval),
    ]
