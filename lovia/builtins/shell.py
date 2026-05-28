"""Subprocess-backed shell tool.

::

    from lovia.builtins.shell import Shell
    shell = Shell(cwd="/work", timeout=30, needs_approval=True)
    agent = Agent(name="x", tools=[shell.tool()])

Defaults to ``needs_approval=True`` because shell access is dangerous.
For unattended / CI use, pass ``needs_approval=False`` or a predicate
that only flags risky commands::

    shell = Shell(
        needs_approval=lambda args, ctx: args["cmd"].startswith(("rm ", "sudo ")),
    )
"""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from typing import Annotated, Any

from ..run_context import RunContext
from ..tools import ApprovalPredicate, Tool, tool

__all__ = ["ApprovalPredicate", "Shell", "allowlist"]


@dataclass
class Shell:
    cwd: str | None = None
    timeout: float = 30.0
    needs_approval: bool | ApprovalPredicate = True
    name: str = "shell"

    def tool(self) -> Tool:
        cwd = self.cwd
        timeout = self.timeout

        @tool(name=self.name, needs_approval=self.needs_approval, timeout=timeout + 5)
        async def _shell(
            cmd: Annotated[str, "Command to run via /bin/sh -c (single string)."],
        ) -> dict[str, Any]:
            """Execute ``cmd`` and return ``{exit_code, stdout, stderr}``."""
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"timeout after {timeout}s",
                }
            return {
                "exit_code": proc.returncode or 0,
                "stdout": stdout_b.decode("utf-8", errors="replace"),
                "stderr": stderr_b.decode("utf-8", errors="replace"),
            }

        return _shell


# Convenience: build a command-allowlist predicate.
def allowlist(commands: list[str]) -> ApprovalPredicate:
    """Build a predicate that asks for approval unless the leading binary
    is in ``commands``.

    Example::

        Shell(needs_approval=allowlist(["ls", "cat", "git"]))
    """
    safe = set(commands)

    def predicate(args: dict[str, Any], ctx: RunContext) -> bool:
        try:
            leading = shlex.split(args.get("cmd", ""))[0]
        except (IndexError, ValueError):
            return True
        return leading not in safe

    return predicate
