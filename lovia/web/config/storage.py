"""Where ``config.json`` lives, and how it is read and written.

One file: ``<home>/config.json``, where the home is ``~/.lovia`` unless
``lovia web --home DIR`` names another. Nothing resolves against the
directory the server was started in. Writes are atomic and owner-only, and
the home is made ``0700`` with a ``*`` .gitignore — the file holds API keys
and must never be committed or world-readable.
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
    "or write config.json in lovia's home (~/.lovia, or --home DIR) by hand"
)

# lovia's home: config, chat history, memory and the general workspace.
DEFAULT_HOME = Path("~/.lovia")
CONFIG_NAME = "config.json"


def config_path(home: Path = DEFAULT_HOME) -> Path:
    """The configuration file inside ``home``."""
    return home.expanduser() / CONFIG_NAME


def display_path(path: Path) -> str:
    """``path`` for people: under the user's home directory it reads
    ``~/…`` (``~/.lovia/config.json``), anywhere else in full."""
    path = Path(os.path.abspath(path.expanduser()))
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


@dataclass
class LoadedConfig:
    """The configuration document and where it came from.

    When the home has no file yet, ``config`` is empty and ``exists`` is
    False; ``path`` is still where the first save goes.
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


def load_active(home: Path = DEFAULT_HOME) -> LoadedConfig:
    """The configuration in ``home`` (empty when it has no file yet)."""
    path = config_path(home)
    config = load_config(path)
    if config is not None:
        return LoadedConfig(config, path, display_path(path), exists=True)
    _warn_legacy_env_file(home)
    return LoadedConfig(WebConfig(), path, display_path(path), exists=False)


def config_writable(path: Path) -> bool:
    """Whether :func:`save_config` could write ``path`` right now.

    The write is a temp file beside the target plus a rename, so the
    deciding permission is the directory's — and the file's own, when it
    exists (a read-only mount refuses both). A directory not created yet
    is writable when its nearest existing ancestor is.
    """
    if path.exists() and not os.access(path, os.W_OK):
        return False
    directory = path.parent
    while not directory.exists():
        if directory.parent == directory:
            return False
        directory = directory.parent
    return directory.is_dir() and os.access(directory, os.W_OK | os.X_OK)


def save_config(config: WebConfig, path: Path | None = None) -> Path:
    """Write the whole document atomically, owner-only (``0600``)."""
    path = path or config_path()
    protect_home(path.parent)
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


_OWN_HOME_HINT = (
    "give lovia a directory of its own, e.g. --home ./.lovia (it writes the "
    "protections itself), or add a line `*` to that directory's .gitignore"
)


def protect_home(directory: Path) -> None:
    """Create lovia's home, make it owner-only, and drop a ``*`` .gitignore.

    The home holds saved secrets and the chat DB, so it's ``0700`` where the OS
    honours it (Windows ignores mode bits) and its whole contents are
    git-ignored — never committed inside someone's repo.

    It must be lovia's own directory: one whose ``.gitignore`` already says
    something else, or a repository's root, is refused before anything is
    changed — writing the key there would leave it committable, and taking
    the directory over (``0700``, ignore everything) would break the project.
    """
    if (directory / ".git").exists():
        raise UserError(
            f"lovia's home can't be a Git repository's root: {directory}",
            hint=_OWN_HOME_HINT,
        )
    gitignore = directory / ".gitignore"
    if gitignore.is_file():
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
        if "*" not in (line.strip() for line in lines):
            raise UserError(
                f"{gitignore} doesn't ignore everything, so the API keys in "
                "config.json and the chat history could be committed",
                hint=_OWN_HOME_HINT,
            )
    directory.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):  # best-effort; Windows ignores mode bits
        directory.chmod(0o700)
    if not gitignore.exists():
        gitignore.write_text(
            "# lovia data — secrets and chat history; never commit.\n*\n",
            encoding="utf-8",
        )


def _warn_legacy_env_file(home: Path) -> None:
    """One heads-up when only a pre-0.9.37 ``config.env`` is around.

    Those files are no longer read (the model connection lives in
    ``config.json`` now); without this line the old configuration would just
    silently stop applying.
    """
    legacy = home.expanduser() / "config.env"
    if legacy.is_file():
        log.warning(
            "found legacy %s — no longer read since 0.9.37; reconnect the "
            "model in the web UI (Settings → Models writes config.json)",
            display_path(legacy),
        )
