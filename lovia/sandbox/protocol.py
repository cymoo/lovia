"""Protocols for the sandbox layer.

A :class:`Sandbox` is an isolated workspace + process environment. A
:class:`SandboxProvider` owns the lifecycle of one or more sandboxes,
typically keyed by ``session_id`` so multi-turn conversations reuse the
same workspace.

Both are :class:`typing.Protocol` interfaces — implementations need not
subclass anything. Local, Docker, K8s, and remote backends all plug in
the same way.
"""

from __future__ import annotations

from typing import (
    AsyncContextManager,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from .types import DirEntry, ExecLimits, ExecResult

__all__ = ["Sandbox", "SandboxProvider"]


@runtime_checkable
class Sandbox(Protocol):
    """Workspace + process execution surface.

    All paths are workspace-relative POSIX strings or absolute paths under
    the logical ``workspace`` root (``"/workspace"`` by default).
    Implementations MUST reject traversal escapes by raising
    :class:`~lovia.sandbox.errors.PathEscape`.

    Lifecycle:

    * Cheap to construct; resources are bound in ``__aenter__`` or on
      first use.
    * :meth:`close` is idempotent and releases any held resources.
    * Instances are safe to use as ``async with`` blocks.
    """

    id: str
    workspace: str  # in-sandbox absolute path, e.g. "/workspace"

    # ---- filesystem ------------------------------------------------------

    async def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        """Return the bytes of ``path``. Raises ``ToolError`` on missing/oversize."""
        ...

    async def write(self, path: str, data: bytes | str, *, append: bool = False) -> int:
        """Write ``data`` to ``path``. Returns bytes written."""
        ...

    async def ls(
        self,
        path: str = ".",
        *,
        max_depth: int = 1,
        include_hidden: bool = False,
    ) -> list[DirEntry]:
        """List entries directly under ``path`` (sorted by name).

        Dotfiles are hidden by default (Unix convention). Pass
        ``include_hidden=True`` to see them — useful when debugging
        the LLM's own ``.venv`` / ``.lovia`` bookkeeping.
        """
        ...

    async def glob(self, pattern: str, *, include_hidden: bool = False) -> list[str]:
        """Return sorted workspace-relative paths matching ``pattern``.

        Paths whose any segment starts with ``.`` are filtered out unless
        ``include_hidden=True`` — this keeps ``**/*.py`` from drowning in
        the LLM's own ``.venv`` site-packages.
        """
        ...

    async def exists(self, path: str) -> bool:
        """Return True if ``path`` exists inside the sandbox."""
        ...

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        """Remove ``path`` (file or, with ``recursive=True``, directory tree)."""
        ...

    # ---- process ---------------------------------------------------------

    async def exec(
        self,
        cmd: "str | Sequence[str]",
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: "str | bytes | None" = None,
        limits: ExecLimits | None = None,
    ) -> ExecResult:
        """Run ``cmd`` inside the sandbox and return the result."""
        ...

    # ---- lifecycle -------------------------------------------------------

    async def close(self) -> None:
        """Release any held resources. Idempotent."""
        ...


@runtime_checkable
class SandboxProvider(Protocol):
    """Owns the lifecycle of one or more :class:`Sandbox` instances.

    Providers cache sandboxes by ``key`` (typically ``session_id``) and
    refcount them so multiple agents in the same session share a workspace.
    Cleanup happens automatically when the refcount reaches zero (or via
    :meth:`shutdown` for global teardown).
    """

    async def acquire(self, key: str) -> Sandbox:
        """Return a sandbox for ``key``, creating it on first call.

        Subsequent calls with the same key return the same instance and
        increment a refcount; release must be called as many times as
        acquire to actually free the sandbox.
        """
        ...

    async def release(self, key: str) -> None:
        """Decrement the refcount; free the sandbox when it hits zero."""
        ...

    async def get(self, key: str) -> "Sandbox | None":
        """Return the cached sandbox for ``key`` without changing refcount."""
        ...

    async def shutdown(self) -> None:
        """Close every cached sandbox. Idempotent."""
        ...

    def session(self, key: str) -> AsyncContextManager[Sandbox]:
        """``async with provider.session(key) as sb: ...`` convenience.

        Most implementations wrap :meth:`acquire` / :meth:`release` with
        ``@asynccontextmanager`` — see :class:`LocalSandboxProvider`.
        """
        ...
