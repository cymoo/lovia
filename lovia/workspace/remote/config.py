"""The ``WorkspaceLike`` config for a directory on a remote host."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from ..policy import WorkspacePolicy
from ..types import RootFacts, WorkspaceLimits
from .host import RemoteHost
from .session import RemoteWorkspaceSession

if TYPE_CHECKING:
    from ...plugins.base import ViewInjector
    from ...tools import Tool

__all__ = ["RemoteWorkspace"]


@dataclass(frozen=True)
class RemoteWorkspace:
    """A directory on a :class:`RemoteHost` the agent's tools operate in.

    The remote counterpart of ``LocalWorkspace``, with the same fields minus
    the two that are Python callables and so cannot run on the far side (a
    policy's ``command_decider``, a shell ``executor``). Build one via
    :meth:`Workspace.remote <lovia.workspace.Workspace.remote>`.

    ``root`` is a path on the host — absolute, or ``~``-prefixed — and must
    exist there. ``env`` and ``inherit_env`` are about the far side's
    environment, not this process's.
    """

    host: RemoteHost
    root: str
    policy: WorkspacePolicy = field(default_factory=WorkspacePolicy.coding)
    env: Mapping[str, str] | None = None
    shell_timeout: float | None = 300.0
    limits: WorkspaceLimits = field(default_factory=WorkspaceLimits)
    inherit_env: bool = False
    close_after_run: bool = True

    async def open(self) -> RemoteWorkspaceSession:
        """Open a session on the host, connecting to it first if need be."""
        return await self.host.open(
            self.root,
            policy=self.policy,
            env=self.env,
            shell_timeout=self.shell_timeout,
            limits=self.limits,
            inherit_env=self.inherit_env,
        )

    def tools(self) -> list["Tool"]:
        from ..workspace import policy_tools

        return policy_tools(self.policy)

    def view_injectors(self) -> list["ViewInjector"]:
        from ..workspace import policy_view_injectors

        return policy_view_injectors(self.policy)

    def instructions(self) -> str:
        """The workspace fragment of the system prompt.

        What it says about the root itself (a git repository? uv-managed?)
        comes from the far side, reported when a session opens — which the
        runner does before it builds the prompt. Asked earlier than that, it
        describes a plain directory.
        """
        from ..workspace import render_instructions

        facts = self.host.facts(self.root) or RootFacts(
            name=PurePosixPath(self.root).name or self.root
        )
        return render_instructions(self.policy, facts)
