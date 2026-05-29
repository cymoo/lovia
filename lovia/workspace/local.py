"""Local filesystem-backed workspace implementation.

``LocalWorkspace`` confines lovia file operations under ``root`` and runs
commands with that directory as the default cwd. It is not a security
boundary: spawned processes run as the host user.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from ..exceptions import ToolError, UserError
from .errors import WorkspaceClosed
from .paths import normalize, resolve
from .types import DirEntry, ExecLimits, ExecResult

__all__ = ["LocalWorkspace"]

_META_DIR = ".lovia"
_PYTHON_DIR = "python"
_PYTHON_TOOL_RE = re.compile(
    r"(^|[;&|()\s])(?:python3?|pip3?|pytest|mypy|ruff|uv|poetry)(?:\s|$)"
)


def _has_hidden_segment(rel: str) -> bool:
    return any(seg.startswith(".") for seg in rel.split("/") if seg)


def _cache_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    base = os.environ.get("XDG_CACHE_HOME")
    return Path(base) if base else Path.home() / ".cache"


@dataclass
class LocalWorkspace:
    """In-process workspace rooted at a host directory."""

    root: str | Path
    workspace: str = "/workspace"
    max_bytes: int = 1_000_000
    env: Mapping[str, str] | None = None
    create: bool = False
    ephemeral: bool = False
    env_isolation: bool = False
    adaptive_python: bool = True
    id: str = field(default_factory=lambda: f"local-{uuid.uuid4().hex[:8]}")
    _root: Path = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _python_ready: bool = field(default=False, init=False, repr=False)
    _python_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        p = Path(self.root).expanduser()
        if self.create:
            p.mkdir(parents=True, exist_ok=True)
        self._root = p.resolve()
        if not self._root.is_dir():
            raise UserError(
                f"Workspace root does not exist: {self._root}",
                hint="Pass create=True or point to an existing directory.",
            )
        self._python_ready = self._python_bin.exists()
        if self.env_isolation:
            self._meta_home.mkdir(parents=True, exist_ok=True)
            self._meta_tmp.mkdir(parents=True, exist_ok=True)

    @property
    def _meta_root(self) -> Path:
        return self._root / _META_DIR

    @property
    def _meta_home(self) -> Path:
        return self._meta_root / "home"

    @property
    def _meta_tmp(self) -> Path:
        return self._meta_root / "tmp"

    @property
    def _python_root(self) -> Path:
        root_key = hashlib.sha256(str(self._root).encode("utf-8")).hexdigest()[:16]
        return _cache_dir() / "lovia" / "workspace-python" / root_key / _PYTHON_DIR

    @property
    def _python_bin(self) -> Path:
        return self._python_root / ("Scripts" if os.name == "nt" else "bin")

    async def __aenter__(self) -> "LocalWorkspace":
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
            raise WorkspaceClosed(f"Workspace {self.id} is closed.")

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

    async def write(
        self,
        path: str,
        data: bytes | str,
        *,
        append: bool = False,
        overwrite: bool = True,
    ) -> int:
        self._check_open()
        payload = data.encode("utf-8") if isinstance(data, str) else data
        p = resolve(self._root, path, workspace=self.workspace)

        def _write() -> int:
            if p.exists() and not overwrite and not append:
                raise ToolError(f"File already exists: {path}")
            old = b""
            if append and p.exists():
                old = p.read_bytes()
            combined = old + payload
            if len(combined) > self.max_bytes:
                raise ToolError(
                    f"Payload too large ({len(combined)} > {self.max_bytes} bytes)."
                )
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", dir=str(p.parent))
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(combined)
                os.replace(tmp, p)
            finally:
                if tmp.exists():
                    tmp.unlink()
            return len(payload)

        return await asyncio.to_thread(_write)

    async def edit(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
    ) -> int:
        self._check_open()
        if old_text == "":
            raise ToolError("old_text must not be empty.")
        raw = await self.read(path)
        text = raw.decode("utf-8", errors="replace")
        count = text.count(old_text)
        if count == 0:
            raise ToolError(
                "edit_file found 0 matches for old_text.",
                hint="Re-read the file and try again with the exact current text.",
            )
        if not replace_all and count != 1:
            raise ToolError(
                f"edit_file found {count} matches for old_text.",
                hint="Use a longer old_text span or set replace_all=True.",
            )
        updated = text.replace(old_text, new_text, -1 if replace_all else 1)
        await self.write(path, updated)
        return count if replace_all else 1

    async def list_dir(
        self,
        path: str = ".",
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[DirEntry]:
        self._check_open()
        p = resolve(self._root, path, workspace=self.workspace)
        if not p.is_dir():
            raise ToolError(f"Not a directory: {path}")

        def _list() -> list[DirEntry]:
            entries: list[DirEntry] = []
            with os.scandir(p) as it:
                for entry in it:
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    if len(entries) >= max_results:
                        raise ToolError(
                            f"Too many directory entries (> {max_results}).",
                            hint="Use a narrower path or increase max_results.",
                        )
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

        return await asyncio.to_thread(_list)

    async def glob(
        self,
        pattern: str,
        *,
        include_hidden: bool = False,
        max_results: int = 1_000,
    ) -> list[str]:
        self._check_open()
        rel_pattern = normalize(pattern, workspace=self.workspace)

        def _glob() -> list[str]:
            results: list[str] = []
            for p in self._root.glob(rel_pattern):
                rel = p.relative_to(self._root).as_posix()
                if not include_hidden and _has_hidden_segment(rel):
                    continue
                if len(results) >= max_results:
                    raise ToolError(
                        f"Too many glob results (> {max_results}).",
                        hint="Use a narrower pattern or increase max_results.",
                    )
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

    def _command_needs_python(self, command: str | Sequence[str]) -> bool:
        if isinstance(command, str):
            return bool(_PYTHON_TOOL_RE.search(command))
        if not command:
            return False
        return Path(str(command[0])).name in {
            "python",
            "python3",
            "pip",
            "pip3",
            "pytest",
            "mypy",
            "ruff",
            "uv",
            "poetry",
        }

    async def _ensure_python(self) -> None:
        if self._python_ready:
            return
        async with self._python_lock:
            if self._python_ready:
                return

            def _create() -> None:
                self._meta_root.mkdir(parents=True, exist_ok=True)
                try:
                    venv.EnvBuilder(with_pip=True, symlinks=True).create(
                        self._python_root
                    )
                except (OSError, subprocess.CalledProcessError) as exc:
                    raise ToolError(
                        "Failed to create managed Python environment.",
                        hint=(
                            "Check that Python's venv/ensurepip support works, or run "
                            "the command with adaptive_python=False."
                        ),
                    ) from exc

            await asyncio.to_thread(_create)
            self._python_ready = True

    def _build_env(
        self,
        override: Mapping[str, str] | None,
        *,
        include_python: bool = False,
    ) -> dict[str, str]:
        if self.env is None:
            base = dict(os.environ)
        else:
            base = dict(self.env)
        if self.env_isolation:
            self._meta_home.mkdir(parents=True, exist_ok=True)
            self._meta_tmp.mkdir(parents=True, exist_ok=True)
            base["HOME"] = str(self._meta_home)
            base["TMPDIR"] = str(self._meta_tmp)
            base["TEMP"] = str(self._meta_tmp)
            base["TMP"] = str(self._meta_tmp)
            base.setdefault("PIP_CACHE_DIR", str(self._meta_home / ".cache" / "pip"))
            base.setdefault("XDG_CACHE_HOME", str(self._meta_home / ".cache"))
        if include_python and self._python_ready:
            existing_path = base.get("PATH", os.defpath)
            base["PATH"] = f"{self._python_bin}{os.pathsep}{existing_path}"
        if override:
            base.update(override)
        return base

    async def exec(
        self,
        command: str | Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: str | bytes | None = None,
        limits: ExecLimits | None = None,
    ) -> ExecResult:
        self._check_open()
        limits = limits or ExecLimits()
        use_managed_python = self.adaptive_python and self._command_needs_python(
            command
        )
        if use_managed_python:
            await self._ensure_python()
        run_cwd = (
            resolve(self._root, cwd, workspace=self.workspace) if cwd else self._root
        )
        merged_env = self._build_env(env, include_python=use_managed_python)

        stdin_bytes = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
        common = dict(
            cwd=str(run_cwd),
            env=merged_env,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        if isinstance(command, str):
            proc = await asyncio.create_subprocess_shell(command, **common)  # type: ignore[arg-type]
        else:
            proc = await asyncio.create_subprocess_exec(*command, **common)  # type: ignore[arg-type]

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
            return bytes(buf), truncated

        async def _run() -> tuple[bytes, bytes, bool]:
            if stdin_bytes is not None and proc.stdin is not None:
                proc.stdin.write(stdin_bytes)
                with contextlib.suppress(BrokenPipeError):
                    await proc.stdin.drain()
                proc.stdin.close()
            out_task = asyncio.create_task(
                _drain_capped(proc.stdout, limits.max_output_bytes)
            )
            err_task = asyncio.create_task(
                _drain_capped(proc.stderr, limits.max_output_bytes)
            )
            so, so_trunc = await out_task
            se, se_trunc = await err_task
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
                exit_code=None,
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
