"""Agent tools that operate on a :class:`Sandbox` or :class:`SandboxProvider`.

:func:`sandbox_tools` returns a list of :class:`~lovia.tools.Tool` you can
attach to an :class:`~lovia.Agent`. When passed a single :class:`Sandbox`,
the tools bind to it directly (per-run scope). When passed a
:class:`SandboxProvider`, the tools lazily resolve the sandbox for
``ctx.session_id`` on first call — this is the lazy-init story the plan
calls for; no extra wiring required.

Tools shipped:

* ``read_file`` — text read.
* ``write_file`` — text write.
* ``list_dir`` — sorted entries with size/mtime.
* ``glob_paths`` — workspace-relative pattern matching.
* ``apply_patch`` — minimal unified-diff editor on top of read+write.
* ``run`` — shell command via the sandbox's ``exec``.
"""

from __future__ import annotations

import difflib
from typing import Annotated, Any, Iterable, Mapping, Protocol

from ..exceptions import ToolError
from ..run_context import RunContext
from ..tools import Tool, ToolPolicy, tool
from .audit import AuditPolicy, AuditStream, AuditToolPolicy
from .protocol import Sandbox, SandboxProvider
from .types import ExecLimits

__all__ = ["sandbox_tools"]


# A "resolver" knows how to produce a sandbox given a RunContext. We accept
# either a bare Sandbox (used directly) or a SandboxProvider (looked up by
# session_id).
class _SandboxResolver(Protocol):
    async def __call__(self, ctx: RunContext) -> Sandbox: ...


def _resolver_for(sb_or_provider: Sandbox | SandboxProvider) -> _SandboxResolver:
    # Importing here keeps a public surface that allows duck-typed Providers.
    if hasattr(sb_or_provider, "acquire") and hasattr(sb_or_provider, "release"):
        provider = sb_or_provider

        async def from_provider(ctx: RunContext) -> Sandbox:
            key = ctx.session_id or "default"
            sb = await provider.get(key)  # type: ignore[union-attr]
            if sb is None:
                sb = await provider.acquire(key)  # type: ignore[union-attr]
            return sb

        return from_provider

    sandbox = sb_or_provider

    async def from_sandbox(ctx: RunContext) -> Sandbox:
        return sandbox  # type: ignore[return-value]

    return from_sandbox


# ---------------------------------------------------------------------------
# apply_patch: small, dependency-free unified diff applier
# ---------------------------------------------------------------------------


def _apply_unified_diff(original: str, patch: str) -> str:
    """Apply a unified diff (``--- / +++ / @@``) to ``original`` text.

    Tolerant: missing trailing newlines, CRLF, and slightly off line numbers
    are all handled by re-locating the hunk by context. Raises ToolError
    on irreparable failures so the model can fix its patch.
    """
    orig_lines = original.splitlines(keepends=True)
    patch_lines = patch.splitlines(keepends=False)

    # Skip headers like ``--- a/foo`` / ``+++ b/foo`` so callers can pass a
    # patch as produced by ``diff -u``.
    i = 0
    while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
        i += 1
    if i == len(patch_lines):
        raise ToolError("Patch contains no @@ hunk header.")

    result: list[str] = []
    cursor = 0

    while i < len(patch_lines):
        header = patch_lines[i]
        if not header.startswith("@@"):
            raise ToolError(f"Expected hunk header, got: {header!r}")
        # Parse @@ -a,b +c,d @@
        try:
            old_part = header.split(" ")[1]
            old_start = int(old_part.lstrip("-").split(",")[0]) - 1
        except (IndexError, ValueError) as exc:
            raise ToolError(f"Malformed hunk header: {header!r}") from exc

        i += 1
        hunk_old: list[str] = []
        hunk_new: list[str] = []
        while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
            line = patch_lines[i]
            if line.startswith(" "):
                hunk_old.append(line[1:])
                hunk_new.append(line[1:])
            elif line.startswith("-"):
                hunk_old.append(line[1:])
            elif line.startswith("+"):
                hunk_new.append(line[1:])
            elif line == "":
                hunk_old.append("")
                hunk_new.append("")
            else:
                # Unknown prefix — be strict so we don't silently corrupt.
                raise ToolError(f"Unexpected patch line: {line!r}")
            i += 1

        # Re-locate hunk if old_start drifted (common when the model
        # numbers things slightly off). Search forward up to 200 lines.
        anchor = old_start
        needle = [s.rstrip("\n") for s in hunk_old]
        if not needle:
            # Pure-insertion hunk (e.g. new file or appended block).
            located = max(cursor, min(anchor, len(orig_lines)))
        else:
            search_from = max(cursor, anchor - 5)
            located = None
            for probe in range(
                search_from, min(len(orig_lines), search_from + 200) + 1
            ):
                if probe + len(needle) > len(orig_lines):
                    break
                window = [
                    orig_lines[probe + k].rstrip("\n") for k in range(len(needle))
                ]
                if window == needle:
                    located = probe
                    break
            if located is None:
                raise ToolError(
                    "Patch hunk does not match source text.",
                    hint="Re-read the file and produce a fresh patch.",
                )

        # Copy the gap, then emit replacement.
        result.extend(orig_lines[cursor:located])
        for new in hunk_new:
            result.append(new + "\n" if not new.endswith("\n") else new)
        cursor = (located or 0) + len(hunk_old)

    result.extend(orig_lines[cursor:])
    return "".join(result)


def _make_simple_diff(before: str, after: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


_DEFAULT_TOOL_NAMES = (
    "read_file",
    "write_file",
    "list_dir",
    "glob_paths",
    "apply_patch",
    "run",
)


def sandbox_tools(
    sb_or_provider: Sandbox | SandboxProvider,
    *,
    audit: AuditPolicy | None = None,
    audit_stream: AuditStream | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    exec_limits: ExecLimits | None = None,
    extra_run_policies: Iterable[ToolPolicy] = (),
) -> list[Tool]:
    """Return agent-facing tools backed by a sandbox or provider.

    Args:
        sb_or_provider: A :class:`Sandbox` (per-run) or
            :class:`SandboxProvider` (per-session lazy resolution).
        audit: Optional :class:`AuditPolicy` to wrap the ``run`` tool. When
            set together with ``audit_stream`` every decision is published
            for UI consumption.
        audit_stream: Optional stream for live audit verdict updates.
        include / exclude: Limit which tools are returned by name.
        exec_limits: Per-call execution limits applied to ``run``.
        extra_run_policies: Additional :class:`ToolPolicy` chained around
            ``run`` (composed after the audit policy).
    """
    resolver = _resolver_for(sb_or_provider)
    wanted = set(include) if include else set(_DEFAULT_TOOL_NAMES)
    if exclude:
        wanted -= set(exclude)
    limits = exec_limits or ExecLimits()

    @tool
    async def read_file(
        ctx: RunContext,
        path: Annotated[str, "Workspace-relative file path."],
    ) -> str:
        """Read a UTF-8 text file from the sandbox."""
        sb = await resolver(ctx)
        return (await sb.read(path)).decode("utf-8", errors="replace")

    @tool
    async def write_file(
        ctx: RunContext,
        path: Annotated[str, "Workspace-relative destination path."],
        content: Annotated[str, "UTF-8 text to write (overwrites by default)."],
        append: Annotated[bool, "Append instead of overwrite."] = False,
    ) -> str:
        """Write a UTF-8 text file in the sandbox."""
        sb = await resolver(ctx)
        n = await sb.write(path, content, append=append)
        return f"wrote {n} bytes to {path}"

    @tool
    async def list_dir(
        ctx: RunContext,
        path: Annotated[str, "Workspace-relative directory, '.' for the root."] = ".",
        include_hidden: Annotated[
            bool, "Show dotfiles (e.g. .venv) the LLM created. Default false."
        ] = False,
    ) -> list[dict[str, Any]]:
        """List directory entries with size and mtime."""
        sb = await resolver(ctx)
        entries = await sb.ls(path, include_hidden=include_hidden)
        return [
            {"name": e.name, "is_dir": e.is_dir, "size": e.size, "mtime": e.mtime}
            for e in entries
        ]

    @tool
    async def glob_paths(
        ctx: RunContext,
        pattern: Annotated[str, "Glob pattern, e.g. '**/*.py'."],
        include_hidden: Annotated[
            bool, "Traverse into dot-directories like .venv. Default false."
        ] = False,
    ) -> list[str]:
        """Return workspace-relative paths matching ``pattern``."""
        sb = await resolver(ctx)
        return await sb.glob(pattern, include_hidden=include_hidden)

    @tool
    async def apply_patch(
        ctx: RunContext,
        path: Annotated[str, "Target file in the sandbox."],
        patch: Annotated[str, "Unified diff (``@@`` hunks)."],
    ) -> str:
        """Apply a unified-diff patch to a sandboxed text file.

        Returns the resulting diff actually written so the model can see
        precisely what changed. Raises ToolError when the patch cannot be
        located.
        """
        sb = await resolver(ctx)
        try:
            original = (await sb.read(path)).decode("utf-8")
        except ToolError:
            original = ""
        updated = _apply_unified_diff(original, patch)
        await sb.write(path, updated)
        return _make_simple_diff(original, updated, path) or "no changes"

    @tool(timeout=(limits.timeout or 30.0) + 5)
    async def run(
        ctx: RunContext,
        cmd: Annotated[str, "Shell command line executed via /bin/sh -c."],
    ) -> dict[str, Any]:
        """Run a shell command in the sandbox.

        The sandbox redirects ``HOME``/``TMPDIR`` to a private dir and
        prepends ``.venv/bin`` to ``PATH``. When Python deps are needed,
        bootstrap a venv first::

            python -m venv .venv && .venv/bin/pip install <pkg>

        Subsequent commands automatically resolve ``python``/``pip`` to
        the venv. Returns ``{exit_code, stdout, stderr, timed_out, truncated}``.
        """
        sb = await resolver(ctx)
        result = await sb.exec(cmd, limits=limits)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }

    all_tools = {
        "read_file": read_file,
        "write_file": write_file,
        "list_dir": list_dir,
        "glob_paths": glob_paths,
        "apply_patch": apply_patch,
        "run": run,
    }

    # Wrap run with the audit policy if configured.
    if audit is not None and "run" in wanted:
        audit_policy = AuditToolPolicy(policy=audit, stream=audit_stream)
        run_tool = all_tools["run"]
        run_tool.policies = (
            audit_policy,
            *tuple(extra_run_policies),
            *run_tool.policies,
        )
        all_tools["run"] = run_tool

    return [all_tools[name] for name in _DEFAULT_TOOL_NAMES if name in wanted]


# Silence unused-import warnings for re-exported aliases.
_ = (Mapping,)
