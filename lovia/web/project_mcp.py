"""A project's own MCP servers: ``.mcp.json`` in the chat's directory.

The file is the one other clients read (``{"mcpServers": {name: {...}}}``),
a convention like ``AGENTS.md`` and ``.agents/skills`` — not a setting. But
unlike those it names commands to run, and it arrives with the repository,
so nothing in it runs until the person at the browser turned that server on
— once, for that definition:

- the decision is kept in lovia's home (``mcp-projects.json``), never in
  the project, so a repository cannot approve itself;
- it is tied to a digest of the entry as written: an edited entry — by a
  pull, or by the agent — is undecided again;
- an undecided or turned-off server is simply not started; the run goes on
  without it.

``${VAR}`` and ``${VAR:-default}`` in an entry are filled from the server's
environment when the run starts, so secrets stay out of the file. They are
not part of the digest: the approval is for what is written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config.mcp import parse_server
from .config.schema import MCPServerConfig

__all__ = [
    "DECISIONS_FILE",
    "PROJECT_MCP_FILE",
    "ProjectMCP",
    "ProjectServer",
    "read_project_servers",
]

log = logging.getLogger("lovia.web.mcp")

PROJECT_MCP_FILE = ".mcp.json"
# In lovia's home. No agent tool may touch it (builder._home_secrets): an
# agent that could write it would turn its own server on.
DECISIONS_FILE = "mcp-projects.json"
# Far more than any real file; a guard against reading a mistake whole.
_MAX_BYTES = 256 * 1024

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

State = Literal["on", "off", "pending"]


@dataclass(frozen=True)
class ProjectServer:
    """One entry of a project's ``.mcp.json``."""

    name: str
    # The entry as written: what is shown for approval, and what is approved.
    spec: Any
    digest: str
    # Ready to start (variables filled in), or None with ``error`` saying why.
    config: MCPServerConfig | None = None
    error: str | None = None


def _digest(spec: Any) -> str:
    text = json.dumps(spec, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    """``value`` with every ``${VAR}`` in its strings filled from ``env``.
    Raises ``ValueError`` naming a variable that is unset and has no default."""
    if isinstance(value, str):

        def fill(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ValueError(f"the variable {name} is not set")

        return _VAR.sub(fill, value)
    if isinstance(value, list):
        return [_expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, env) for k, v in value.items()}
    return value


def read_project_servers(
    root: Path, env: Mapping[str, str] | None = None
) -> tuple[list[ProjectServer], str | None]:
    """The servers ``root``'s ``.mcp.json`` names, and what is wrong with the
    file as a whole (``None``: nothing, or there is no file).

    An entry that can't be used — a bad shape, an unset variable, one the
    file itself disables — comes back with its ``error`` rather than
    failing the others. A stdio server starts in ``root``, or in its own
    ``cwd`` taken relative to it.
    """
    env = os.environ if env is None else env
    path = root / PROJECT_MCP_FILE
    try:
        # A link out of the project would make it someone else's file.
        if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            return [], None
        if path.stat().st_size > _MAX_BYTES:
            return [], f"{PROJECT_MCP_FILE} is over {_MAX_BYTES // 1024} KB"
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], f"{PROJECT_MCP_FILE} can't be read: {exc}"
    entries = doc.get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(entries, dict):
        return [], f'{PROJECT_MCP_FILE} has no "mcpServers" object'
    servers = []
    for name, spec in entries.items():
        name = str(name)
        config, error = None, None
        try:
            config = parse_server(name, _expand(spec, env))
            if not config.enabled:
                config, error = None, "the file turns it off (disabled)"
            elif config.command is not None:
                # In the project, or where the entry says relative to it.
                config = config.model_copy(
                    update={"cwd": str(root / (config.cwd or ""))}
                )
        except ValueError as exc:
            error = str(exc).removeprefix(f"{name}: ")
        servers.append(ProjectServer(name, spec, _digest(spec), config, error))
    return servers, None


class ProjectMCP:
    """Which servers are on in which project: :data:`DECISIONS_FILE` in
    lovia's home.

    ``{"projects": {<project key>: {<server name>: {"on": bool, "digest":
    str}}}}``. A decision counts for the entry it was made on: another
    digest is undecided.
    """

    def __init__(self, home: Path) -> None:
        self.path = home / DECISIONS_FILE

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            # Unreadable: every server is undecided again — never on.
            log.warning("%s can't be read (%s); ignoring it", self.path, exc)
            return {}
        projects = doc.get("projects") if isinstance(doc, dict) else None
        return projects if isinstance(projects, dict) else {}

    def states(self, project: str, servers: list[ProjectServer]) -> dict[str, State]:
        """Each of ``servers``' state in ``project``, by name."""
        decided = self._load().get(project)
        decided = decided if isinstance(decided, dict) else {}
        out: dict[str, State] = {}
        for server in servers:
            entry = decided.get(server.name)
            if not isinstance(entry, dict) or entry.get("digest") != server.digest:
                out[server.name] = "pending"
            else:
                out[server.name] = "on" if entry.get("on") is True else "off"
        return out

    def decide(self, project: str, server: ProjectServer, on: bool) -> None:
        projects = self._load()
        if not isinstance(projects.get(project), dict):
            projects[project] = {}
        projects[project][server.name] = {
            "on": on,
            "digest": server.digest,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "projects": projects}, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def approved(self, project: str) -> list[MCPServerConfig]:
        """The servers of ``project`` (a local directory) to start now:
        turned on, as they are written today, and usable."""
        servers, _ = read_project_servers(Path(project))
        if not servers:
            return []
        states = self.states(project, servers)
        return [
            s.config for s in servers if s.config is not None and states[s.name] == "on"
        ]
