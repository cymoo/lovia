"""Where ``config.json`` lives, and how it is read and written.

Two scopes, project first: ``./.lovia/config.json`` beats
``~/.lovia/config.json`` as a whole file (no per-key merging — the active
file *is* the configuration). Writes are atomic and owner-only, and a
``.lovia/`` target directory is made ``0700`` with a ``*`` .gitignore — the
file holds API keys and must never be committed or world-readable.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ...exceptions import UserError
from .schema import WebConfig

log = logging.getLogger("lovia.web.config")

# How to get configured — pointed at whenever the model is missing.
CONFIG_HINT = (
    "start `lovia web` and open the printed URL — Settings → Models — "
    "or write ~/.lovia/config.json by hand"
)

# lovia's data dir beside the chat DB — a lovia-owned home, so it never
# collides with another tool's files and stays tidy when launched from $HOME.
CONFIG_DIR = Path(".lovia")
CONFIG_NAME = "config.json"

PROJECT_CONFIG_LABEL = ".lovia/config.json"
USER_CONFIG_LABEL = "~/.lovia/config.json"


def project_config_path() -> Path:
    """Project-scope config (``./.lovia/config.json``) — this directory only."""
    return CONFIG_DIR / CONFIG_NAME


def user_config_path() -> Path:
    """User-scope config (``~/.lovia/config.json``) — used from any directory."""
    return Path("~/.lovia").expanduser() / CONFIG_NAME


@dataclass
class LoadedConfig:
    """The active configuration document and where it came from.

    When neither scope has a file yet, ``config`` is empty, ``exists`` is
    False, and ``path``/``label`` point at the user scope — the default save
    target for a first-run setup.
    """

    config: WebConfig
    path: Path
    label: str
    exists: bool


def load_config(path: Path) -> WebConfig | None:
    """Parse one config file; ``None`` when absent, :class:`UserError` when broken."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UserError(f"cannot read {path}: {exc}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise UserError(
            f"invalid JSON in {path}: {exc}",
            hint="fix the file (or delete it and rerun `lovia web --setup`)",
        ) from exc
    try:
        return WebConfig.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "config"
        raise UserError(
            f"invalid configuration in {path}: {where}: {first.get('msg', exc)}",
            hint="fix the file (or delete it and rerun `lovia web --setup`)",
        ) from exc


def load_active() -> LoadedConfig:
    """The effective configuration: the project file if present, else the user's."""
    for path, label in (
        (project_config_path(), PROJECT_CONFIG_LABEL),
        (user_config_path(), USER_CONFIG_LABEL),
    ):
        config = load_config(path)
        if config is not None:
            return LoadedConfig(config, path, label, exists=True)
    _warn_legacy_env_files()
    return LoadedConfig(
        WebConfig(), user_config_path(), USER_CONFIG_LABEL, exists=False
    )


def save_config(config: WebConfig, path: Path | None = None) -> Path:
    """Write the whole document atomically, owner-only (``0600``)."""
    path = path or user_config_path()
    if path.parent.name == CONFIG_DIR.name:
        _protect_config_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    payload = config.model_dump(mode="json", exclude_none=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{CONFIG_NAME}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        with suppress(OSError):  # best-effort; Windows ignores mode bits
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
    return path


def _protect_config_dir(directory: Path) -> None:
    """Create lovia's data dir, make it owner-only, and drop a ``*`` .gitignore.

    The dir holds saved secrets and the chat DB, so it's ``0700`` where the OS
    honours it (Windows ignores mode bits) and its whole contents are
    git-ignored — never committed inside someone's repo.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):  # best-effort; Windows ignores mode bits
        directory.chmod(0o700)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# lovia data — secrets and chat history; never commit.\n*\n",
            encoding="utf-8",
        )


def _warn_legacy_env_files() -> None:
    """One heads-up when only a pre-0.9.37 ``config.env`` is around.

    Those files are no longer read (the model connection lives in
    ``config.json`` now); without this line the old configuration would just
    silently stop applying.
    """
    legacy = [
        label
        for path, label in (
            (CONFIG_DIR / "config.env", "./.lovia/config.env"),
            (Path("~/.lovia/config.env").expanduser(), "~/.lovia/config.env"),
        )
        if path.is_file()
    ]
    if legacy:
        log.warning(
            "found legacy %s — no longer read since 0.9.37; reconnect the "
            "model in the web UI (Settings → Models writes config.json)",
            " and ".join(legacy),
        )
