"""Local filesystem-backed sandbox.

:class:`LocalSandbox` is the **honest minimal** local backend: it confines
file operations under a directory and runs commands via
:func:`asyncio.create_subprocess_*`. It is *not* a security boundary — the
spawned processes can do anything the host user can. Use it for development,
tests, and trusted code; for untrusted code use a real container backend.

**Dependency isolation (the interesting bit).**
Each sandbox redirects ``HOME`` and ``TMPDIR`` to a private subdirectory
(``<root>/.lovia/home`` and ``<root>/.lovia/tmp``) and prepends
``<root>/.venv/bin`` to ``PATH``. The framework does **not** create or
manage that venv — the LLM does, by simply running::

    python -m venv .venv && .venv/bin/pip install pandas

From the next command onwards, ``python`` and ``pip`` on ``PATH`` resolve
to the venv's binaries automatically. No special API, no auto-bootstrap,
no surprise mutations to the host environment. The audit policy ships
with a warn-rule that gently nudges the LLM toward this pattern when it
forgets.

:class:`LocalSandboxProvider` owns a pool of LocalSandboxes keyed by
``session_id`` with refcounting and ephemeral cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Mapping, Sequence

from ..exceptions import ToolError, UserError
from .errors import SandboxClosed
from .paths import resolve
from .types import DirEntry, ExecLimits, ExecResult

__all__ = ["LocalSandbox", "LocalSandboxProvider", "single_sandbox_provider"]


_META_DIR = ".lovia"
_VENV_DIR = ".venv"


def _has_hidden_segment(rel: str) -> bool:
    """True iff any path segment of ``rel`` starts with a dot."""
    return any(seg.startswith(".") for seg in rel.split("/") if seg)


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


@dataclass
class LocalSandbox:
    """In-process sandbox rooted at a host directory.

    Args:
        root: Host directory all operations are confined to. Created if
            missing when ``create=True``.
        workspace: Logical, in-sandbox path the agent sees. ``/workspace``
            by default so prompts stay portable for Docker.
        max_bytes: Hard ceiling for ``read`` / ``write`` payloads.
        env: Base environment for ``exec``. ``None`` inherits the host
            environment; pass ``{}`` for a clean baseline. The sandbox
            always overrides ``HOME``, ``TMPDIR``, and prefixes ``PATH``
            on top of whatever is supplied.
        create: Create ``root`` if missing.
        ephemeral: When True, ``close`` deletes the root directory.
        id: Optional stable id; auto-generated when not supplied.
    """

    root: str | Path
    workspace: str = "/workspace"
    max_bytes: int = 1_000_000
    env: Mapping[str, str] | None = None
    create: bool = False
    ephemeral: bool = False
    id: str = field(default_factory=lambda: f"local-{uuid.uuid4().hex[:8]}")
    _root: Path = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        p = Path(self.root).expanduser()
        if self.create:
            p.mkdir(parents=True, exist_ok=True)
        self._root = p.resolve()
        if not self._root.is_dir():
            raise UserError(
                f"Sandbox root does not exist: {self._root}",
                hint="Pass create=True or point to an existing directory.",
            )
        # Framework-managed bookkeeping dirs. The LLM's .venv lives at the
        # workspace root, not under .lovia/, so it shows up as a normal
        # project artifact the model can reason about.
        (self._root / _META_DIR / "home").mkdir(parents=True, exist_ok=True)
        (self._root / _META_DIR / "tmp").mkdir(parents=True, exist_ok=True)

    # ---- internal paths -------------------------------------------------

    @property
    def _meta_home(self) -> Path:
        return self._root / _META_DIR / "home"

    @property
    def _meta_tmp(self) -> Path:
        return self._root / _META_DIR / "tmp"

    @property
    def _venv_bin(self) -> Path:
        return self._root / _VENV_DIR / "bin"

    # ---- context manager ------------------------------------------------

    async def __aenter__(self) -> "LocalSandbox":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.ephemeral:
            await asyncio.to_thread(shutil.rmtree, self._root, ignore_errors=True)

    def _check_open(self) -> None:
        if self._closed:
            raise SandboxClosed(f"Sandbox {self.id} is closed.")

    # ---- filesystem -----------------------------------------------------

    async def read(self, path: str, *, max_bytes: int | None = None) -> bytes:
        self._check_open()
        limit = max_bytes if max_bytes is not None else self.max_bytes
        p = resolve(self._root, path, workspace=self.workspace)
        if not p.is_file():
            raise ToolError(f"Not a file: {path}")

        def _read() -> bytes:
            with p.open("rb") as fh:
                data = fh.read(limit + 1)
            if len(data) > limit:
                raise ToolError(
                    f"File too large (> {limit} bytes).",
                    hint="Raise max_bytes or read a slice.",
                )
            return data

        return await asyncio.to_thread(_read)

    async def write(self, path: str, data: bytes | str, *, append: bool = False) -> int:
        self._check_open()
        payload = data.encode("utf-8") if isinstance(data, str) else data
        if len(payload) > self.max_bytes:
            raise ToolError(
                f"Payload too large ({len(payload)} > {self.max_bytes} bytes)."
            )
        p = resolve(self._root, path, workspace=self.workspace)

        def _write() -> int:
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if append else "wb"
            with p.open(mode) as fh:
                fh.write(payload)
            return len(payload)

        return await asyncio.to_thread(_write)

    async def ls(
        self,
        path: str = ".",
        *,
        max_depth: int = 1,
        include_hidden: bool = False,
    ) -> list[DirEntry]:
        self._check_open()
        p = resolve(self._root, path, workspace=self.workspace)
        if not p.is_dir():
            raise ToolError(f"Not a directory: {path}")

        def _ls() -> list[DirEntry]:
            entries: list[DirEntry] = []
            with os.scandir(p) as it:
                for entry in it:
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    try:
                        stat = entry.stat()
                        size = stat.st_size if entry.is_file() else None
                        mtime = stat.st_mtime
                    except OSError:
                        size, mtime = None, None
                    entries.append(
                        DirEntry(
                            name=entry.name,
                            is_dir=entry.is_dir(),
                            size=size,
                            mtime=mtime,
                        )
                    )
            entries.sort(key=lambda e: (not e.is_dir, e.name))
            return entries

        return await asyncio.to_thread(_ls)

    async def glob(self, pattern: str, *, include_hidden: bool = False) -> list[str]:
        self._check_open()
        if pattern.startswith("/") or ".." in pattern.split("/"):
            raise ToolError("Glob pattern must be workspace-relative without '..'.")
        root = self._root

        def _glob() -> list[str]:
            results: list[str] = []
            for p in root.glob(pattern):
                rel = p.relative_to(root).as_posix()
                if not include_hidden and _has_hidden_segment(rel):
                    continue
                results.append(rel)
            return sorted(results)

        return await asyncio.to_thread(_glob)

    async def exists(self, path: str) -> bool:
        if self._closed:
            return False
        try:
            p = resolve(self._root, path, workspace=self.workspace)
        except Exception:
            return False
        return await asyncio.to_thread(p.exists)

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        self._check_open()
        p = resolve(self._root, path, workspace=self.workspace)

        def _remove() -> None:
            if not p.exists():
                return
            if p.is_dir():
                if not recursive:
                    raise ToolError(
                        f"Refusing to remove directory without recursive=True: {path}"
                    )
                shutil.rmtree(p)
            else:
                p.unlink()

        await asyncio.to_thread(_remove)

    # ---- process --------------------------------------------------------

    def _build_env(self, override: Mapping[str, str] | None) -> dict[str, str]:
        """Merge: host (or self.env) ← lovia overrides ← caller override.

        Lovia always sets HOME/TMPDIR to the per-sandbox dirs and prepends
        the LLM-managed venv to PATH. Caller can still override anything
        in ``override`` — that's their explicit choice.
        """
        if self.env is None:
            base = dict(os.environ)
        else:
            base = dict(self.env)
        base["HOME"] = str(self._meta_home)
        base["TMPDIR"] = str(self._meta_tmp)
        base["TEMP"] = str(self._meta_tmp)
        base["TMP"] = str(self._meta_tmp)
        existing_path = base.get("PATH", os.defpath)
        base["PATH"] = f"{self._venv_bin}{os.pathsep}{existing_path}"
        # Pip/uv obey these — keep their caches inside the sandbox too,
        # so even a stray bare ``pip install --user`` doesn't pollute the
        # host's ~/.cache.
        base.setdefault("PIP_CACHE_DIR", str(self._meta_home / ".cache" / "pip"))
        base.setdefault("XDG_CACHE_HOME", str(self._meta_home / ".cache"))
        if override:
            base.update(override)
        return base

    async def exec(
        self,
        cmd: "str | Sequence[str]",
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: "str | bytes | None" = None,
        limits: ExecLimits | None = None,
    ) -> ExecResult:
        self._check_open()
        limits = limits or ExecLimits()
        run_cwd = (
            resolve(self._root, cwd, workspace=self.workspace) if cwd else self._root
        )
        merged_env = self._build_env(env)

        stdin_bytes: bytes | None = None
        if stdin is not None:
            stdin_bytes = stdin.encode("utf-8") if isinstance(stdin, str) else stdin

        # ``start_new_session`` so killing the leader on timeout also kills
        # any child processes the command spawned.
        common = dict(
            cwd=str(run_cwd),
            env=merged_env,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if isinstance(cmd, str):
            proc = await asyncio.create_subprocess_shell(cmd, **common)  # type: ignore[arg-type]
        else:
            proc = await asyncio.create_subprocess_exec(*cmd, **common)  # type: ignore[arg-type]

        async def _drain_capped(
            stream: asyncio.StreamReader | None, cap: int
        ) -> tuple[bytes, bool]:
            if stream is None:
                return b"", False
            buf = bytearray()
            truncated = False
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break
                if len(buf) < cap:
                    take = cap - len(buf)
                    buf.extend(chunk[:take])
                if len(buf) >= cap:
                    truncated = True
                    # Keep draining so the child doesn't stall on a full
                    # OS pipe buffer.
                    continue
            return bytes(buf), truncated

        async def _run() -> tuple[bytes, bytes, bool]:
            if stdin_bytes is not None and proc.stdin is not None:
                proc.stdin.write(stdin_bytes)
                with contextlib.suppress(BrokenPipeError):
                    await proc.stdin.drain()
                proc.stdin.close()
            so, so_trunc = await _drain_capped(proc.stdout, limits.max_output_bytes)
            se, se_trunc = await _drain_capped(proc.stderr, limits.max_output_bytes)
            await proc.wait()
            return so, se, so_trunc or se_trunc

        try:
            stdout_b, stderr_b, truncated = await asyncio.wait_for(
                _run(), timeout=limits.timeout
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=f"[timeout after {limits.timeout}s]",
                timed_out=True,
            )

        return ExecResult(
            exit_code=proc.returncode or 0,
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
            truncated=truncated,
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


@dataclass
class _Entry:
    sandbox: LocalSandbox
    refcount: int = 0


@dataclass
class LocalSandboxProvider:
    """Pool of :class:`LocalSandbox` instances keyed by string ``key``.

    Args:
        root_base: Parent directory where per-key roots are created.
            Defaults to a process-unique tempdir.
        ephemeral: If True (default), per-key roots are deleted on release.
        workspace: Logical workspace path passed to each sandbox.
        env: Default ``env`` for created sandboxes.
    """

    root_base: str | Path | None = None
    ephemeral: bool = True
    workspace: str = "/workspace"
    env: Mapping[str, str] | None = None
    _entries: dict[str, _Entry] = field(default_factory=dict, init=False, repr=False)
    _base: Path = field(init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.root_base is None:
            self._base = Path(tempfile.mkdtemp(prefix="lovia-sandbox-")).resolve()
        else:
            self._base = Path(self.root_base).expanduser().resolve()
            self._base.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self) -> "LocalSandboxProvider":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.shutdown()

    def _key_root(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key) or "default"
        return self._base / safe

    async def acquire(self, key: str) -> LocalSandbox:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                root = self._key_root(key)
                root.mkdir(parents=True, exist_ok=True)
                sb = LocalSandbox(
                    root=root,
                    workspace=self.workspace,
                    env=self.env,
                    ephemeral=self.ephemeral,
                    id=f"local-{key}",
                )
                entry = _Entry(sandbox=sb)
                self._entries[key] = entry
            entry.refcount += 1
            return entry.sandbox

    async def release(self, key: str) -> None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry.refcount -= 1
            if entry.refcount <= 0:
                sb = entry.sandbox
                del self._entries[key]
            else:
                sb = None
        if sb is not None:
            await sb.close()

    async def get(self, key: str) -> LocalSandbox | None:
        async with self._lock:
            entry = self._entries.get(key)
            return entry.sandbox if entry is not None else None

    async def shutdown(self) -> None:
        async with self._lock:
            sandboxes = [e.sandbox for e in self._entries.values()]
            self._entries.clear()
        for sb in sandboxes:
            await sb.close()
        if self.ephemeral and self._base.exists():
            await asyncio.to_thread(shutil.rmtree, self._base, ignore_errors=True)

    @asynccontextmanager
    async def session(self, key: str) -> AsyncIterator[LocalSandbox]:
        sb = await self.acquire(key)
        try:
            yield sb
        finally:
            await self.release(key)


# ---------------------------------------------------------------------------
# Per-run convenience
# ---------------------------------------------------------------------------


@dataclass
class _SingleSandboxProvider:
    """Adapter that exposes one already-built sandbox as a Provider."""

    sandbox: LocalSandbox

    async def acquire(self, key: str) -> LocalSandbox:
        return self.sandbox

    async def release(self, key: str) -> None:
        return None

    async def get(self, key: str) -> LocalSandbox | None:
        return self.sandbox

    async def shutdown(self) -> None:
        await self.sandbox.close()

    @asynccontextmanager
    async def session(self, key: str) -> AsyncIterator[LocalSandbox]:
        yield self.sandbox


def single_sandbox_provider(sb: LocalSandbox) -> _SingleSandboxProvider:
    """Wrap a single :class:`LocalSandbox` as a :class:`SandboxProvider`.

    Useful for per-run usage where you don't want pooling.
    """
    return _SingleSandboxProvider(sb)
