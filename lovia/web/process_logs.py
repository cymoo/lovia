"""Finished background commands on disk: one file each, in the chat's folder
under ``process-logs/``.

A file is one line of JSON (:class:`ProcessRecord`), then the output as it
was. They are a cache, not data: the folder may be deleted at any time, and
the chats there simply show no history. Everything here blocks; callers run
it in a thread.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal

__all__ = ["EndedBy", "ProcessRecord", "load_records", "read_record", "save_record"]

EndedBy = Literal["exit", "user", "model", "shutdown", "closed", "lost"]
"""What ended a command: itself, the user's stop, the model's
``kill_process``, the server going down, its workspace session closing (the
chat moved to another directory), or the connection to its host dropping."""

# Records kept per chat; writing one more drops the oldest.
_MAX_RECORDS = 30
_MAX_COMMAND_CHARS = 4000
_DEFAULT_MAX_MB = 300

# A process id becomes a file name.
_PROCESS_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True, kw_only=True)
class ProcessRecord:
    """How one background command ended. ``base`` is the offset of the first
    character kept, so a file is paged by the offsets its buffer had."""

    id: str
    command: str
    cwd: str
    started_at: float
    ended_at: float
    status: Literal["exited", "killed"]
    exit_code: int | None
    ended_by: EndedBy
    base: int
    chars: int


def max_total_bytes() -> int:
    """Cap on all records together (env ``LOVIA_MAX_PROCESS_LOGS_MB``,
    default 300), applied by the hourly sweep."""
    try:
        mb = int(os.environ.get("LOVIA_MAX_PROCESS_LOGS_MB", ""))
    except ValueError:
        mb = _DEFAULT_MAX_MB
    return max(0, mb) * 1024 * 1024


def _path(folder: Path, process_id: str) -> Path | None:
    if not _PROCESS_ID.fullmatch(process_id):
        return None
    return folder / f"{process_id}.log"


def save_record(folder: Path, record: ProcessRecord, text: str) -> None:
    path = _path(folder, record.id)
    if path is None:
        return
    head = asdict(record)
    head["command"] = record.command[:_MAX_COMMAND_CHARS]
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{record.id}.tmp"
    tmp.write_bytes(
        json.dumps(head, ensure_ascii=False).encode("utf-8")
        + b"\n"
        + text.encode("utf-8")
    )
    os.replace(tmp, path)
    for old, _, _ in _oldest_first(folder.glob("*.log"))[:-_MAX_RECORDS]:
        old.unlink(missing_ok=True)


def load_records(folder: Path) -> list[ProcessRecord]:
    """Every readable record in ``folder``; the output is not read."""
    out = []
    try:
        paths = list(folder.glob("*.log"))
    except OSError:
        return []
    for path in paths:
        try:
            with path.open("rb") as f:
                out.append(_parse(f.readline()))
        except (OSError, ValueError):
            continue  # swept meanwhile, or not ours
    return out


def read_record(folder: Path, process_id: str) -> tuple[ProcessRecord, str] | None:
    """One record and its output."""
    path = _path(folder, process_id)
    if path is None:
        return None
    try:
        head, _, body = path.read_bytes().partition(b"\n")
        return _parse(head), body.decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def _parse(line: bytes) -> ProcessRecord:
    """Raises ``ValueError`` for a line that is not a record's."""
    data = json.loads(line)
    try:
        return ProcessRecord(**{f.name: data[f.name] for f in fields(ProcessRecord)})
    except (KeyError, TypeError) as exc:
        raise ValueError("not a process record") from exc


def trim_total(folders: list[Path], max_bytes: int) -> None:
    """Delete the oldest records in ``folders`` until they fit."""
    files = _oldest_first(path for folder in folders for path in folder.glob("*.log"))
    total = sum(size for _, _, size in files)
    for path, _, size in files:
        if total <= max_bytes:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:  # held open (Windows): the next sweep
            continue
        total -= size


def _oldest_first(paths: Iterable[Path]) -> list[tuple[Path, float, int]]:
    """Each file that is still there: path, mtime, size."""
    out = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        out.append((path, st.st_mtime, st.st_size))
    return sorted(out, key=lambda item: item[1])
