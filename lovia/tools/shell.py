"""Built-in workspace ``shell`` tool.

Approval is decided by the workspace policy: commands the policy marks
``ask`` go through the human-approval channel, ``deny`` is refused inside
the session (the model sees the error and can adjust), and ``allow`` runs
immediately.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..run_context import RunContext
from ..workspace.types import CommandResult
from .base import tool
from .files import require_workspace

__all__ = ["shell"]


def _shell_needs_approval(args: dict[str, Any], ctx: RunContext[Any]) -> bool:
    workspace = ctx.workspace
    if workspace is None:
        return False  # the tool itself will fail with a setup hint
    command = args.get("command")
    if not isinstance(command, str):
        return False  # malformed args fail validation later
    return workspace.policy.decide_command(command) == "ask"


def _render_command_result(result: Any, ctx: RunContext[Any]) -> Any:
    if not isinstance(result, CommandResult):
        return result
    if result.timed_out:
        return f"command timed out\n{result.stderr}".strip()
    parts = [f"exit code: {result.exit_code}"]
    if result.stdout.strip():
        parts.append(result.stdout.rstrip("\n"))
    if result.stderr.strip():
        parts.append(f"--- stderr ---\n{result.stderr.rstrip(chr(10))}")
    if len(parts) == 1:
        parts.append("(no output)")
    return "\n".join(parts)


@tool(
    name="shell",
    description=(
        "Run a one-shot, non-interactive shell command in the workspace.\n"
        "- cwd is workspace-relative; the command starts there. Quote paths "
        "that contain spaces.\n"
        "- No TTY and no interactive input: never run editors, REPLs, "
        "watchers, or anything that prompts (use non-interactive flags like "
        "--yes instead). Long-running commands are killed at the timeout.\n"
        "- stdout/stderr are captured and truncated when large; pipe through "
        "filters (grep, head, tail) to keep output focused.\n"
        "- The command runs as the host user, so it is NOT sandboxed: "
        "destructive or out-of-workspace commands may be denied by policy or "
        "require user approval. Never run destructive commands (rm -rf, "
        "git reset --hard, force-push, ...) unless the user explicitly asked.\n"
        "- Prefer the dedicated tools over shell equivalents: read_file over "
        "cat, grep_files over grep/rg, list_files over ls/find, edit_file "
        "over sed."
    ),
    needs_approval=_shell_needs_approval,
    result_renderer=_render_command_result,
)
async def shell(
    ctx: RunContext[Any],
    command: Annotated[str, "Shell command line to run."],
    cwd: Annotated[str, "Workspace-relative working directory."] = ".",
    timeout: Annotated[
        float | None,
        Field(default=None, ge=1, description="Override timeout in seconds."),
    ] = None,
) -> CommandResult:
    return await require_workspace(ctx).run(command, cwd=cwd, timeout=timeout)
