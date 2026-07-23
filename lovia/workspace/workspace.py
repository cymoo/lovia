"""User-facing workspace configuration.

:class:`Workspace` is a thin **factory facade**: it does not hold state
itself, it builds a backend-specific configuration that implements
:class:`~lovia.workspace.protocol.WorkspaceLike`::

    from lovia.workspace import Workspace

    agent = Agent(
        name="coder",
        workspace=Workspace.local(".", mode="coding"),
    )

``Workspace.local(...)`` returns a :class:`LocalWorkspace` (the local-FS
backend). Future backends grow as sibling factories — ``Workspace.docker(...)``
etc. — each returning its own ``WorkspaceLike`` config. The runner only ever
depends on the protocol, so adding a backend touches neither the runner nor
the tools.

The runner opens a session per run, injects it into ``RunContext.workspace``
(where the built-in tools find it), and closes sessions it owns. Use
:meth:`LocalWorkspace.session` to keep one session alive across runs.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator, Mapping, NoReturn

from ..exceptions import UserError
from .local import LocalWorkspaceSession
from .policy import CommandRule, Decision, PathRule, WorkspacePolicy
from .protocol import ShellExecutor, WorkspaceSession
from .types import WorkspaceLimits, WorkspaceMode

if TYPE_CHECKING:
    from ..tools import Tool

__all__ = ["LocalWorkspace", "Workspace"]


def _has_tool_table(text: str, tool: str) -> bool:
    """True if ``pyproject.toml`` declares a ``[tool.<tool>]`` table (or a
    sub-table like ``[tool.<tool>.sources]``).

    Line-anchored on purpose: a real TOML header is the first non-space token
    on its line, so this won't fire on the tool name buried in a comment or a
    string value. ``[tool.uvicorn]`` doesn't match ``uv`` — the ']' / '.' after
    the name is part of the compared prefix.
    """
    head, sub = f"[tool.{tool}]", f"[tool.{tool}."
    return any(
        (s := line.strip()).startswith(head) or s.startswith(sub)
        for line in text.splitlines()
    )


def _python_pkg_flavor(root: Path) -> str:
    """Which Python package manager owns this workspace: 'uv', 'poetry', 'pip'.

    Read from lockfiles and ``pyproject.toml`` markers so the shell guidance
    names the tool that actually works here. The stakes are highest for uv:
    ``uv venv`` deliberately omits pip, so a generic "python/pip resolve to the
    venv" story sends the model down three dead ends ('.venv/bin/pip' missing,
    'python -m pip' with no pip module, host pip with a broken interpreter).
    Lockfiles are the strongest signal; a ``[tool.<mgr>]`` table is the
    fallback for a project that hasn't locked yet.
    """
    if (root / "uv.lock").is_file():
        return "uv"
    if (root / "poetry.lock").is_file():
        return "poetry"
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text("utf-8", "ignore")
        except OSError:
            return "pip"
        if _has_tool_table(text, "uv"):
            return "uv"
        if _has_tool_table(text, "poetry"):
            return "poetry"
    return "pip"


@dataclass(frozen=True)
class LocalWorkspace:
    """A local directory the agent's file/shell tools operate in.

    A lightweight backend config implementing ``WorkspaceLike``: the runner
    opens a session for each run and closes the sessions it owns. The policy
    gates what the tools may do; it is honest scoping, not OS-level isolation
    — see :mod:`lovia.workspace.policy`. Build one via :meth:`Workspace.local`.
    """

    root: str
    policy: WorkspacePolicy = field(default_factory=WorkspacePolicy.coding)
    env: Mapping[str, str] | None = None
    shell_timeout: float | None = 300.0
    limits: WorkspaceLimits = field(default_factory=WorkspaceLimits)
    inherit_env: bool = False
    executor: ShellExecutor | None = None
    close_after_run: bool = True

    async def open(self) -> LocalWorkspaceSession:
        """Open a live workspace session."""
        return LocalWorkspaceSession(
            root=self.root,
            policy=self.policy,
            env=self.env,
            shell_timeout=self.shell_timeout,
            limits=self.limits,
            inherit_env=self.inherit_env,
            executor=self.executor,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator["_WorkspaceSessionBinding"]:
        """Open a user-owned session that survives across multiple runs.

        The yielded binding is accepted by ``Agent.workspace``; the runner
        reuses the live session instead of opening a fresh one per run.
        """
        session = await self.open()
        try:
            yield _WorkspaceSessionBinding(workspace=self, _session=session)
        finally:
            await session.close()

    def tools(self) -> list["Tool"]:
        """Return the built-in tool bundle permitted by this policy."""
        from .tools import (
            edit_file,
            grep_files,
            list_files,
            read_file,
            shell,
            write_file,
        )

        bundle: list["Tool"] = [read_file, list_files, grep_files]
        if self.policy.write != "deny" or self.policy.write_outside != "deny":
            bundle += [write_file, edit_file]
        if self.policy.allow_shell:
            bundle.append(shell)
        return bundle

    def instructions(self) -> str:
        """Render the workspace fragment appended to the system prompt.

        Derived from the policy so the prompt states what is actually
        enforced — it must never promise more than the session delivers.
        """
        policy = self.policy
        root = Path(self.root).expanduser().resolve()
        root_name = root.name or str(self.root)
        lines = [
            "## Workspace",
            # The name is a label, never a path: presenting it path-like makes
            # models call list_files('<name>') and miss ('.' is the root).
            f"You work in a workspace named {root_name!r}. Its root directory "
            "is '.' — address files with workspace-relative paths ('.', "
            "'notes/plan.md'; preferred) or absolute ones, never with the "
            "workspace name. Symlinks resolve to their targets and are judged "
            "by where they lead.",
            "Explore with list_files and grep_files; read a file before editing "
            "it; use edit_file for targeted changes and write_file for new files "
            "or full rewrites. Large reads and command output are truncated — "
            "page with start/end or narrow the command rather than dumping "
            "everything.",
        ]
        if policy.write == "deny":
            lines.append(
                "The workspace is read-only: write_file and edit_file are "
                "disabled inside it."
            )
        elif policy.write == "ask":
            lines.append(
                "Writes inside the workspace require user approval; batch "
                "related edits rather than asking one line at a time."
            )
        outside = _describe_outside_access(policy.read_outside, policy.write_outside)
        if outside:
            lines.append(outside)
        if policy.denied_paths:
            denied = ", ".join(repr(p) for p in policy.denied_paths)
            lines.append(
                f"Paths matching {denied} are off-limits: file tools refuse "
                "them, and shell commands that name them are refused too."
            )
        if policy.allow_shell:
            if policy.shell_default == "allow":
                lines.append(
                    "A shell tool is available and generally runs without "
                    "approval; it is not OS-sandboxed and runs as the host "
                    "user, so be deliberate with anything destructive or "
                    "irreversible."
                )
            else:
                lines.append(
                    "A shell tool is available but gated: some commands need "
                    "user approval and others are denied by policy."
                )
            lines.append(
                "Paths named in shell commands (arguments, redirect targets, "
                "cwd) are checked against the same rules as the file tools, "
                "so the shell is not a way around them; a command that needs "
                "broader access will ask for approval instead."
            )
            lines.append(
                "Python work stays in the workspace's own virtualenv: when a "
                "real '.venv' (or 'venv') virtualenv exists at the root — one "
                "with an interpreter inside, not just a directory by that "
                "name — it is automatically on PATH for shell commands "
                "(VIRTUAL_ENV set), so 'python' already resolves to it, no "
                "activation needed. Never install into the global environment."
            )
            flavor = _python_pkg_flavor(root)
            if flavor == "uv":
                lines.append(
                    "This project is uv-managed: install packages with "
                    "'uv pip install <pkg>' (straight into the active venv) or "
                    "'uv sync' to materialize the lockfile, and create the venv "
                    "with 'uv venv' when none exists. uv venvs deliberately omit "
                    "pip, so 'pip' and 'python -m pip' will fail — don't reach "
                    "for them."
                )
            elif flavor == "poetry":
                lines.append(
                    "This project is poetry-managed: install with "
                    "'poetry install' (from the lockfile) or 'poetry add <pkg>', "
                    "and run code via 'poetry run ...' unless poetry's virtualenv "
                    "is already active on PATH. Let poetry manage its virtualenv "
                    "rather than building one by hand."
                )
            else:
                lines.append(
                    "python and pip already resolve to the venv. To install "
                    "packages when none exists, create it first (e.g. "
                    "'python -m venv .venv')."
                )
        return "\n".join(lines)


def _describe_outside_access(read: Decision, write: Decision) -> str:
    """One honest sentence about access beyond the workspace root."""
    if read == "deny" and write == "deny":
        return (
            "Access outside the workspace root is not permitted; work within "
            "the workspace."
        )
    phrases = {
        "allow": "is allowed",
        "ask": "requires user approval",
        "deny": "is not permitted",
    }
    return (
        f"Outside the workspace root, reading {phrases[read]} and writing "
        f"{phrases[write]}. When access is denied, ask the user to widen the "
        "workspace configuration rather than working around it."
    )


class Workspace:
    """Factory facade for workspace backends.

    Not instantiated directly — use a backend factory such as
    :meth:`local`. Each factory returns a config object implementing
    :class:`~lovia.workspace.protocol.WorkspaceLike`.
    """

    def __init__(self) -> None:  # pragma: no cover - guard against misuse
        raise UserError(
            "Workspace is a factory, not a backend.",
            hint="Use Workspace.local(...) (or another backend factory).",
        )

    @classmethod
    def local(
        cls,
        root: str = ".",
        *,
        mode: WorkspaceMode = "coding",
        policy: WorkspacePolicy | None = None,
        readable: tuple[str, ...] = (),
        writable: tuple[str, ...] = (),
        denied_paths: tuple[str, ...] = (),
        path_rules: tuple[PathRule, ...] = (),
        command_rules: tuple[CommandRule, ...] = (),
        env: Mapping[str, str] | None = None,
        shell_timeout: float | None = 300.0,
        inherit_env: bool = False,
        limits: WorkspaceLimits | None = None,
        executor: ShellExecutor | None = None,
    ) -> LocalWorkspace:
        """Create a local-filesystem workspace rooted at ``root``.

        ``mode`` selects a policy preset, optionally refined with:

        * ``readable=`` / ``writable=`` — grant access to paths *outside* the
          root (absolute or ``~`` patterns; ``writable`` implies readable),
          e.g. ``readable=("~/docs",)`` for a reference folder.
        * ``denied_paths=`` — patterns no tool may touch (``".env*"``, ...).
        * ``path_rules=`` — full ACL control when the shorthands don't fit.
        * ``command_rules=`` — shell prefix rules (allow/ask/deny).

        Pass an explicit ``policy`` to take full control instead of using a
        preset (mutually exclusive with the rule shorthands above).

        ``inherit_env`` controls the shell environment. By default (``False``)
        only a minimal, non-secret allowlist is passed to commands so
        credentials in the host environment (API keys, tokens) don't leak; pass
        ``inherit_env=True`` to hand the full host env to commands — opt-in for
        every mode, ``trusted`` included. Add specific variables with ``env=``
        regardless.

        .. warning::
           ``inherit_env=True`` exposes **every** host environment variable —
           including ``OPENAI_API_KEY``, cloud credentials, and tokens — to any
           command the model runs. Prefer the default and pass only what a
           command needs via ``env={...}``; reserve ``inherit_env=True`` for
           trusted code in a sandboxed/throwaway environment.

        ``executor`` plugs in an OS-sandboxing command runner (see
        :class:`~lovia.workspace.protocol.ShellExecutor`); ``limits`` tunes
        the tool size/count caps. Omit both for sensible defaults.
        """
        rules = _combine_rules(path_rules, readable=readable, writable=writable)
        if policy is not None:
            if denied_paths or command_rules or rules:
                raise UserError(
                    "Pass either policy= or rule shorthands "
                    "(readable/writable/denied_paths/path_rules/command_rules), "
                    "not both.",
                    hint="Put the rules inside your WorkspacePolicy.",
                )
        elif mode == "readonly":
            if command_rules:
                raise UserError(
                    "mode='readonly' has no shell; command_rules are unused."
                )
            policy = WorkspacePolicy.readonly(
                denied_paths=denied_paths, path_rules=rules
            )
        elif mode == "coding":
            policy = WorkspacePolicy.coding(
                denied_paths=denied_paths,
                path_rules=rules,
                command_rules=command_rules,
            )
        elif mode == "trusted":
            policy = WorkspacePolicy.trusted(
                denied_paths=denied_paths,
                path_rules=rules,
                command_rules=command_rules,
            )
        else:
            raise UserError(f"Unknown workspace mode: {mode!r}")
        return LocalWorkspace(
            root=str(root),
            policy=policy,
            env=dict(env) if env is not None else None,
            shell_timeout=shell_timeout,
            limits=limits if limits is not None else WorkspaceLimits(),
            inherit_env=inherit_env,
            executor=executor,
        )

    @classmethod
    def docker(cls, *args: object, **kwargs: object) -> NoReturn:
        """Placeholder for a future container backend (real OS isolation).

        The :class:`~lovia.workspace.protocol.WorkspaceSession` /
        ``WorkspaceLike`` protocols are the extension point — a container
        backend implements them without touching the runner or the tools.
        """
        raise NotImplementedError(
            "Workspace.docker() is not implemented yet. The local backend "
            "(Workspace.local) is an honest policy gate, not OS isolation; a "
            "container backend implementing the WorkspaceSession/WorkspaceLike "
            "protocols (see lovia.workspace.protocol) is the path to real "
            "isolation."
        )


def _combine_rules(
    path_rules: tuple[PathRule, ...],
    *,
    readable: tuple[str, ...],
    writable: tuple[str, ...],
) -> tuple[PathRule, ...]:
    """Expand the readable/writable shorthands into ACL rules.

    Explicit ``path_rules`` come first so they can override the shorthands;
    ``denied_paths`` always wins regardless (checked before any rule).
    """
    grants = [PathRule(pat, "allow") for pat in writable]
    grants += [PathRule(pat, "allow", ops=frozenset({"read"})) for pat in readable]
    return tuple(path_rules) + tuple(grants)


@dataclass(frozen=True)
class _WorkspaceSessionBinding:
    """A user-owned live session accepted by ``Agent.workspace``."""

    workspace: LocalWorkspace
    _session: WorkspaceSession
    close_after_run: bool = False

    async def open(self) -> WorkspaceSession:
        return self._session

    def tools(self) -> list["Tool"]:
        return self.workspace.tools()

    def instructions(self) -> str:
        return self.workspace.instructions()
