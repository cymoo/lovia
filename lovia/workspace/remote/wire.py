"""What crosses the pipe between a remote host and its far side.

One JSON object per line. JSON-RPC 2.0 in spirit — ``id`` pairs a response
with its request, a message without one is a notification — minus the
envelope fields nothing here reads::

    -> {"id": 7, "method": "read_text", "sid": "local-1a2b", "params": {...}}
    <- {"id": 7, "result": {...}}        or   {"id": 7, "error": {...}}
    -> {"method": "cancel", "params": {"id": 7}}

Values are JSON with a few tagged objects for what JSON cannot say: bytes, a
tuple or set (policy fields are tuples and frozensets, and equality depends
on it), a list that reports truncation, and the dataclasses both sides share.

Runs on both sides, so it is part of the stdlib-only engine (see
``tests/workspace/test_engine_stdlib.py``).
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable
from dataclasses import fields, is_dataclass
from typing import Any

from ...approvals import Verdict
from ...exceptions import LoviaError, ToolError, UserError
from ..errors import (
    FileTooLargeError,
    PermissionDeniedError,
    WorkspaceClosedError,
    WorkspaceDisconnectedError,
    WorkspaceError,
)
from ..policy import CommandRule, PathRule, WorkspacePolicy
from ..types import (
    ClippedList,
    CommandResult,
    DirEntry,
    EditResult,
    FileBytes,
    FileChange,
    FileContent,
    GrepMatch,
    ProcessOutput,
    ProcessStart,
    ProcessStatus,
    RootFacts,
    WorkspaceLimits,
)

__all__ = [
    "MAX_LINE",
    "PROTOCOL",
    "decode",
    "decode_error",
    "encode",
    "encode_error",
    "read_lines",
]

# Bumped on any incompatible change. Both ends run the same source (the near
# side ships it), so a mismatch means a stale far process, never a version
# to negotiate with.
PROTOCOL = 1

# One line can carry a whole file (base64): room for the read cap, and then some.
MAX_LINE = 64 * 1024 * 1024

_TYPES: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        CommandResult,
        CommandRule,
        DirEntry,
        EditResult,
        FileBytes,
        FileChange,
        FileContent,
        GrepMatch,
        PathRule,
        ProcessOutput,
        ProcessStart,
        ProcessStatus,
        RootFacts,
        Verdict,
        WorkspaceLimits,
        WorkspacePolicy,
    )
}

_ERRORS: dict[str, type[Exception]] = {
    cls.__name__: cls
    for cls in (
        FileExistsError,
        FileNotFoundError,
        FileTooLargeError,
        IsADirectoryError,
        NotADirectoryError,
        PermissionDeniedError,
        ToolError,
        UserError,
        WorkspaceClosedError,
        WorkspaceDisconnectedError,
        WorkspaceError,
    )
}


async def read_lines(
    reader: asyncio.StreamReader,
    idle: float | None = None,
    still_alive: Callable[[], bool] | None = None,
) -> AsyncIterator[bytes]:
    """Complete lines off ``reader`` until it ends.

    ``idle`` bounds the wait for *bytes*, not for a line: one line can be a
    whole file crawling over a slow link, and that is a live connection all
    the way through. When nothing at all arrives for ``idle`` seconds,
    ``still_alive`` gets a say (the near side is still alive while it is
    pushing a large request out); otherwise ``asyncio.TimeoutError``.
    """
    buffer, scanned = bytearray(), 0
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(1 << 16), idle)
        except asyncio.TimeoutError:
            if still_alive is not None and still_alive():
                continue
            raise
        if not chunk:
            return
        buffer += chunk
        while (end := buffer.find(b"\n", scanned)) >= 0:
            yield bytes(buffer[:end])
            del buffer[: end + 1]
            scanned = 0
        scanned = len(buffer)
        if scanned > MAX_LINE:
            raise ValueError(f"a message exceeded {MAX_LINE} bytes")


def encode(value: Any) -> Any:
    """``value`` as JSON-ready data. Raises ``TypeError`` for a type the wire
    has no word for, and :class:`UserError` for a policy that cannot travel."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, ClippedList):
        return {"$clipped": [encode(v) for v in value], "truncated": value.truncated}
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, tuple):
        return {"$tuple": [encode(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        return {"$set": sorted(encode(v) for v in value)}
    if isinstance(value, dict):
        return {"$dict": {str(k): encode(v) for k, v in value.items()}}
    if isinstance(value, WorkspacePolicy):
        return _encode_policy(value)
    if is_dataclass(value) and type(value).__name__ in _TYPES:
        return _encode_fields(value)
    raise TypeError(f"cannot send a {type(value).__name__} to a remote workspace")


def _encode_fields(value: Any, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    out: dict[str, Any] = {"$type": type(value).__name__}
    for f in fields(value):
        if f.init and f.name not in skip:
            out[f.name] = encode(getattr(value, f.name))
    return out


def _encode_policy(policy: WorkspacePolicy) -> dict[str, Any]:
    if policy.command_decider is not None:
        raise UserError(
            "A workspace policy with a command_decider cannot be used remotely.",
            hint=(
                "command_decider is a Python callable and the policy is judged "
                "on the far side; express it as command_rules instead."
            ),
        )
    from ..rules import DEFAULT_BASELINE

    out = _encode_fields(policy, skip=("command_decider", "baseline"))
    # The default baseline is built per platform: leave it for the far side
    # to resolve where the files are, instead of shipping this machine's.
    if policy.baseline != DEFAULT_BASELINE:
        out["baseline"] = encode(policy.baseline)
    return out


def decode(value: Any) -> Any:
    """The inverse of :func:`encode`."""
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "$bytes" in value:
        return base64.b64decode(value["$bytes"])
    if "$clipped" in value:
        items = [decode(v) for v in value["$clipped"]]
        return ClippedList(items, truncated=value["truncated"])
    if "$tuple" in value:
        return tuple(decode(v) for v in value["$tuple"])
    if "$set" in value:
        return frozenset(decode(v) for v in value["$set"])
    if "$dict" in value:
        return {k: decode(v) for k, v in value["$dict"].items()}
    cls = _TYPES[value["$type"]]
    return cls(**{k: decode(v) for k, v in value.items() if k != "$type"})


def encode_error(exc: BaseException) -> dict[str, Any]:
    # LoviaError renders its hint into str(); send the two apart.
    hint = getattr(exc, "hint", None)
    message = str(exc.args[0]) if hint is not None and exc.args else str(exc)
    out: dict[str, Any] = {"type": type(exc).__name__, "message": message, "hint": hint}
    if isinstance(exc, OSError) and exc.errno is not None:
        # What the system said, kept apart so the near side can raise the
        # same error (disk full, no permission) instead of a nameless one.
        name = exc.filename if isinstance(exc.filename, str) else None
        out["os"] = [exc.errno, exc.strerror, name]
    return out


def decode_error(error: dict[str, Any]) -> Exception:
    """The far side's exception as the same class here, where there is one;
    otherwise a :class:`WorkspaceError` that names what it was."""
    name, message, hint = error.get("type"), error.get("message", ""), error.get("hint")
    if error.get("os"):
        errno, strerror, filename = error["os"]
        # OSError picks the subclass for the errno: PermissionError, …
        return OSError(errno, strerror, *([filename] if filename else []))
    cls = _ERRORS.get(name or "")
    if cls is None:
        return WorkspaceError(f"{name}: {message}", hint=hint)
    if issubclass(cls, LoviaError):
        return cls(message, hint=hint)
    return cls(message)  # an OSError: FileNotFoundError and its kin
