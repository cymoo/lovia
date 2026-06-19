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
from .policy import CommandRule, WorkspacePolicy
from .protocol import WorkspaceSession
from .types import WorkspaceLimits, WorkspaceMode

if TYPE_CHECKING:
    from ..tools import Tool

__all__ = ["LocalWorkspace", "Workspace", "WorkspaceConfig"]


@dataclass(frozen=True)
class WorkspaceConfig:
    """Backend-agnostic workspace configuration.

    The shared config every backend composes (``LocalWorkspace`` today, a
    future ``DockerWorkspace`` tomorrow), so policy, environment, timeout, and
    size limits are defined once and reused regardless of how the session is
    actually backed.
    """

    policy: WorkspacePolicy = field(default_factory=WorkspacePolicy.coding)
    env: Mapping[str, str] | None = None
    inherit_env: bool = False
    shell_timeout: float | None = 300.0
    limits: WorkspaceLimits = field(default_factory=WorkspaceLimits)


@dataclass(frozen=True)
class LocalWorkspace:
    """A local directory the agent's file/shell tools operate in.

    A lightweight backend config implementing ``WorkspaceLike``: the runner
    opens a session for each run and closes the sessions it owns. The policy
    gates what the tools may do; it is honest scoping, not OS-level isolation
    — see :mod:`lovia.workspace.policy`. Build one via :meth:`Workspace.local`.
    """

    root: str
    config: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    close_after_run: bool = True

    @property
    def policy(self) -> WorkspacePolicy:
        """Convenience accessor for ``config.policy`` (read by tools/prompt)."""
        return self.config.policy

    async def open(self) -> LocalWorkspaceSession:
        """Open a live workspace session."""
        c = self.config
        return LocalWorkspaceSession(
            root=self.root,
            policy=c.policy,
            env=c.env,
            shell_timeout=c.shell_timeout,
            limits=c.limits,
            inherit_env=c.inherit_env,
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
        if self.policy.allow_write:
            bundle += [write_file, edit_file]
        if self.policy.allow_shell:
            bundle.append(shell)
        return bundle

    def instructions(self) -> str:
        """Render the workspace fragment appended to the system prompt."""
        root_name = Path(self.root).expanduser().resolve().name or str(self.root)
        lines = [
            "## Workspace",
            f"You work in a workspace rooted at {root_name!r}. All file paths "
            "and shell working directories are relative to this root; absolute "
            "host paths and '..' escapes above the root are rejected.",
            "Explore with list_files and grep_files; read a file before editing "
            "it; use edit_file for targeted changes and write_file for new files "
            "or full rewrites. Large reads and command output are truncated — "
            "page with start/end or narrow the command rather than dumping "
            "everything.",
        ]
        if not self.policy.allow_write:
            lines.append(
                "This workspace is read-only: write_file and edit_file are "
                "disabled and not offered."
            )
        if self.policy.denied_paths:
            denied = ", ".join(repr(p) for p in self.policy.denied_paths)
            lines.append(
                f"Paths matching {denied} are off-limits to every tool "
                "(including the shell); do not try to read or modify them."
            )
        if self.policy.allow_shell:
            if self.policy.shell_default == "allow":
                lines.append(
                    "A shell tool is available and generally runs without "
                    "approval; it is not sandboxed and runs as the host user, "
                    "so be deliberate with anything destructive or irreversible."
                )
            else:
                lines.append(
                    "A shell tool is available but gated: some commands need "
                    "user approval and others are denied by policy. It is not a "
                    "way around the file rules above — denied paths stay denied."
                )
        return "\n".join(lines)


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
        denied_paths: tuple[str, ...] = (),
        command_rules: tuple[CommandRule, ...] = (),
        env: Mapping[str, str] | None = None,
        shell_timeout: float | None = 300.0,
        inherit_env: bool | None = None,
        limits: WorkspaceLimits | None = None,
    ) -> LocalWorkspace:
        """Create a local-filesystem workspace rooted at ``root``.

        ``mode`` selects a policy preset (optionally refined with
        ``denied_paths`` / ``command_rules``); pass an explicit ``policy`` to
        take full control instead of using a preset.

        ``inherit_env`` controls the shell environment: by default only a
        minimal, non-secret allowlist is passed to commands so credentials in
        the host environment (API keys, tokens) don't leak. Leave it ``None``
        to inherit the full host env only for ``trusted`` workspaces; set it
        explicitly to force the behaviour either way. Add specific variables
        with ``env=`` regardless.

        ``limits`` tunes the tool size/count caps (read pagination, shell
        output, grep/listing); omit for sensible defaults.
        """
        if policy is not None:
            if denied_paths or command_rules:
                raise UserError(
                    "Pass either policy= or denied_paths/command_rules, not both.",
                    hint="Put the rules inside your WorkspacePolicy.",
                )
        elif mode == "readonly":
            if command_rules:
                raise UserError(
                    "mode='readonly' has no shell; command_rules are unused."
                )
            policy = WorkspacePolicy.readonly(denied_paths=denied_paths)
        elif mode == "coding":
            policy = WorkspacePolicy.coding(
                denied_paths=denied_paths, command_rules=command_rules
            )
        elif mode == "trusted":
            policy = WorkspacePolicy.trusted(
                denied_paths=denied_paths, command_rules=command_rules
            )
        else:
            raise UserError(f"Unknown workspace mode: {mode!r}")
        if inherit_env is None:
            # Tie env inheritance to the "trusted" posture: a workspace that
            # runs shell without approval may as well see the host env;
            # everything else defaults to the minimal allowlist.
            inherit_env = bool(policy.allow_shell and policy.shell_default == "allow")
        config = WorkspaceConfig(
            policy=policy,
            env=dict(env) if env is not None else None,
            inherit_env=inherit_env,
            shell_timeout=shell_timeout,
            limits=limits if limits is not None else WorkspaceLimits(),
        )
        return LocalWorkspace(root=str(root), config=config)

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
