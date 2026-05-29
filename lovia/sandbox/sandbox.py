"""User-facing sandbox configuration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Mapping

from .local import LocalSandboxBackend
from .protocol import SandboxBackend, SandboxSession
from .types import SandboxMode, SandboxSpec

__all__ = ["Sandbox"]


_SANDBOX_INSTRUCTIONS = """You have access to a sandbox rooted at the project directory.
Use relative paths for file tools. Do not assume absolute host paths.
Prefer edit_file for targeted changes and write_file for new files or full rewrites.
Shell commands may require approval depending on sandbox mode."""


@dataclass(frozen=True)
class Sandbox:
    """Configuration for an agent sandbox.

    ``Sandbox`` is a lightweight factory. Runner opens a session for each run and
    closes the sessions it owns. For explicit cross-run persistence, use
    :meth:`session`.
    """

    backend: SandboxBackend
    spec: SandboxSpec
    close_on_run: bool = True

    @classmethod
    def local(
        cls,
        root: str = ".",
        *,
        mode: SandboxMode = "coding",
        env: Mapping[str, str] | None = None,
        shell_timeout: float | None = 300.0,
    ) -> "Sandbox":
        """Create a local sandbox rooted at ``root``.

        Local sandboxes confine lovia file tools to ``root`` but are not a hard
        OS security boundary; approved shell commands run as the host user.
        """

        return cls(
            backend=LocalSandboxBackend(),
            spec=SandboxSpec(
                root=str(root),
                mode=mode,
                env=dict(env) if env is not None else None,
                shell_timeout=shell_timeout,
            ),
        )

    @classmethod
    def with_backend(
        cls,
        backend: SandboxBackend,
        *,
        root: str = ".",
        mode: SandboxMode = "coding",
        env: Mapping[str, str] | None = None,
        shell_timeout: float | None = 300.0,
    ) -> "Sandbox":
        """Create a sandbox configuration from a custom backend."""

        return cls(
            backend=backend,
            spec=SandboxSpec(
                root=root,
                mode=mode,
                env=dict(env) if env is not None else None,
                shell_timeout=shell_timeout,
            ),
        )

    @property
    def mode(self) -> SandboxMode:
        return self.spec.mode

    async def open(self) -> SandboxSession:
        """Open a live sandbox session."""

        return await self.backend.open(self.spec)

    @asynccontextmanager
    async def session(self) -> AsyncIterator["_SandboxSessionBinding"]:
        """Open a user-owned sandbox session for multiple runs."""

        session = await self.open()
        try:
            yield _SandboxSessionBinding(session=session, mode=self.mode)
        finally:
            await session.close()

    def tools(self, session: SandboxSession) -> list[object]:
        """Return sandbox tools bound to ``session``."""

        from ..tools import coding_tools

        return list(coding_tools(session=session, mode=self.mode))

    def instructions(self) -> str:
        """Return the sandbox prompt fragment."""

        return _SANDBOX_INSTRUCTIONS


@dataclass(frozen=True)
class _SandboxSessionBinding:
    """A user-owned session handle accepted by ``Agent.sandbox``."""

    session: SandboxSession
    mode: SandboxMode = "coding"
    close_on_run: bool = False

    def __getattr__(self, name: str) -> object:
        return getattr(self.session, name)

    async def open(self) -> SandboxSession:
        return self.session

    def tools(self, session: SandboxSession) -> list[object]:
        from ..tools import coding_tools

        return list(coding_tools(session=session, mode=self.mode))

    def instructions(self) -> str:
        return _SANDBOX_INSTRUCTIONS
