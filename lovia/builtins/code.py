"""Subprocess-backed Python code execution tool.

::

    from lovia.builtins.code import PythonRunner
    runner = PythonRunner(timeout=15, needs_approval=True)
    agent = Agent(name="x", tools=[runner.tool()])

Like :mod:`lovia.builtins.shell`, this defaults to ``needs_approval=True``;
override for unattended use. Code is executed via ``sys.executable -c``
in a temp working directory.

This is **not** a sandbox — it has the same permissions as the host
process. For untrusted code, run inside a real isolation layer
(container, gVisor, Pyodide, ...).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from typing import Annotated, Any, Callable

from ..run_context import RunContext
from ..tools import Tool, tool


ApprovalPredicate = Callable[[dict[str, Any], RunContext], bool]


@dataclass
class PythonRunner:
    timeout: float = 30.0
    needs_approval: bool | ApprovalPredicate = True
    name: str = "python_exec"
    python: str | None = None  # defaults to sys.executable

    def tool(self) -> Tool:
        timeout = self.timeout
        python = self.python or sys.executable

        @tool(name=self.name, needs_approval=self.needs_approval, timeout=timeout + 5)
        async def _exec(
            code: Annotated[str, "Python source to execute."],
        ) -> dict[str, Any]:
            """Run ``code`` in a fresh subprocess. Returns ``{exit_code, stdout, stderr}``."""
            with tempfile.TemporaryDirectory() as tmp:
                proc = await asyncio.create_subprocess_exec(
                    python,
                    "-c",
                    code,
                    cwd=tmp,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    out_b, err_b = await asyncio.wait_for(
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
                "stdout": out_b.decode("utf-8", errors="replace"),
                "stderr": err_b.decode("utf-8", errors="replace"),
            }

        return _exec
