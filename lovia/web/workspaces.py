"""Chat-scoped workspace sessions.

The core runner opens a fresh workspace session per run and closes it when the
run ends (``LocalWorkspace.close_after_run``). For a *chat*, that lifetime is
wrong for one thing only — background processes: a dev server started with
``shell(background=true)`` must outlive the turn that started it, or the
feature cannot serve its headline use case. Everything else (files, policy)
is stateless across runs and does not care.

:class:`WorkspaceSessions` scopes one live session to each web session id:
the supervisor calls :meth:`bind` when a run starts, which lazily opens the
chat's session and hands the runner a :class:`WorkspaceSessionBinding`
(``close_after_run=False``) so every run of that chat reuses it. The session
— and with it every background process — dies when the chat is deleted or
the app shuts down, never merely because a run finished.

Core semantics are untouched: processes are still session-owned and still
ephemeral across restarts; the web layer just holds sessions longer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast, get_args

from ..agent import Agent
from ..exceptions import UserError
from ..workspace import (
    LocalWorkspace,
    RemoteHost,
    RemoteWorkspace,
    Workspace,
    WorkspaceMode,
)
from ..workspace.protocol import WorkspaceLike, WorkspaceSession
from ..workspace.workspace import WorkspaceSessionBinding

log = logging.getLogger(__name__)

__all__ = ["ChatBinding", "WorkspaceRef", "WorkspaceSessions", "WorkspaceSwitch"]

# Narrowest first: a mode may only ever be capped toward the front.
MODE_ORDER: tuple[str, ...] = get_args(WorkspaceMode)


def resolve_root(path: str | Path) -> Path:
    """``path`` as a chat binding stores it: user-expanded, symlinks resolved."""
    return Path(path).expanduser().resolve()


def workspace_config(agent: Agent[Any]) -> LocalWorkspace | None:
    """``agent``'s local-workspace config — through a session binding, whose
    ``.workspace`` is the config — or None for no or a custom workspace."""
    ws = agent.workspace
    cfg = getattr(ws, "workspace", ws)
    return cfg if isinstance(cfg, LocalWorkspace) else None


def own_root(agent: Agent[Any]) -> Path | None:
    """The resolved root of ``agent``'s own local workspace, if it has one."""
    cfg = workspace_config(agent)
    return resolve_root(cfg.root) if cfg is not None else None


@dataclass(frozen=True)
class WorkspaceRef:
    """Where a chat works, taken apart: a local directory, or a host and a
    directory on it.

    A chat stores it as one string — its ``workspace_root``, the same string
    the sidebar groups by and an approval is remembered under. A local
    directory is its resolved path, as ever. A remote one is spelled like a
    URL: ``ssh://box/srv/project`` (an SSH destination), ``docker://api/app``
    or ``docker://dev@api/app`` (a running container, entered as ``dev``),
    ``remote://name/srv/project`` (a host the deployment declared by name).
    Only the few places that open, check or describe a workspace parse it;
    everything else just carries the string.
    """

    root: str
    scheme: str | None = None
    """``None`` for a local directory, else ``ssh`` / ``docker`` / ``remote``."""
    host: str | None = None

    @classmethod
    def parse(cls, key: str) -> "WorkspaceRef":
        scheme, sep, rest = key.partition("://")
        if not sep or scheme not in ("ssh", "docker", "remote"):
            return cls(root=key)
        host, _, path = rest.partition("/")
        # ``host/srv/x`` is /srv/x there; ``host/~/x`` is under its home.
        return cls(
            root=path if path.startswith("~") else "/" + path, scheme=scheme, host=host
        )

    @property
    def remote(self) -> bool:
        return self.scheme is not None

    @property
    def key(self) -> str:
        """The string a chat stores: :meth:`parse` reads it back."""
        if self.scheme is None:
            return self.root
        return f"{self.scheme}://{self.host}/{self.root.lstrip('/')}"

    @property
    def host_name(self) -> str | None:
        """What the host is called — in the page, and in the record of an
        approval: the declared name, or ``ssh:box`` / ``docker:api``."""
        if self.scheme is None:
            return None
        return self.host if self.scheme == "remote" else f"{self.scheme}:{self.host}"


@dataclass(frozen=True)
class ChatBinding:
    """Where and how a chat's runs work, as stored on the chat.

    ``root`` is the resolved directory, or a remote location's key (see
    :class:`WorkspaceRef`); None: the agent has no local workspace. ``mode``
    is the permission mode (None: the agent's own policy).
    """

    root: str | None
    mode: str | None


@dataclass(frozen=True)
class WorkspaceSwitch:
    """Let each chat pick its directory and permission mode:
    ``create_app(workspaces=)``.

    Without it the served agent's workspace is what every chat gets. With it
    a chat is bound to a directory when it is created (its first message's
    ``workspace``, else the agent's own root) and stores a mode — chosen in
    the composer, or with ``workspace_mode`` — and each of its runs gets the
    agent's workspace rebuilt for that directory and mode.

    * ``roots`` — which directories a chat may be bound to: ``"any"``, a
      list (those directories and everything under them), or ``None`` (only
      the agent's own root). The agent's own root and ``general`` are always
      allowed. Checked on every run, so a chat whose directory a narrower
      deployment no longer allows is refused, never moved.
    * ``modes`` — what a chat may pick; ``readonly`` is always among them.
      A stored mode this server doesn't offer (a deployment narrowed since)
      runs as the widest offered mode below it: only ever narrower.
    * ``default_mode`` — what the page preselects for a new chat; ``None``
      runs new chats on the agent's own policy until one is picked.
    * ``build`` — ``(root, mode) -> LocalWorkspace``. The default is the
      ``Workspace.local`` preset for the mode, keeping the agent workspace's
      env, limits, executor, ``inherit_env`` and shell timeout — but not a
      custom policy's rules. Pass ``build`` to carry those across modes.
    * ``agent_for`` — ``(agent, root) -> agent``: the project-specific rest
      (instructions, plugins — say, the directory's ``AGENTS.md`` and
      skills). Called on every run, before the workspace is set; whatever
      workspace it sets is replaced.
    * ``general`` — a catch-all directory for chats that belong to no
      project: always listed, created on first use.
    * ``remotes`` — whether a chat may work somewhere other than this
      machine, the counterpart of ``roots``: ``None`` (the default) for no,
      a mapping of names to :class:`~lovia.workspace.RemoteHost` for those
      hosts only (``remote://name/path``), or ``"any"`` for any SSH
      destination or running container the server's own account can reach
      (``ssh://box/path``, ``docker://api/path``) — which is that account's
      whole reach, so it belongs on a server only its owner can use. A
      remote chat runs with the mode's policy, enforced on the host; the
      Files panel, uploads and attachments are not available to it yet.
    * ``agent_for_remote`` — ``(agent, ref) -> agent``: ``agent_for`` for a
      chat at a remote location (a :class:`WorkspaceRef`). ``agent_for``
      itself is never called for one — it is handed a directory on this
      machine to read. If the served agent already carries a local
      project's instructions or skills, this is where they come off.

    A chat that never picked a mode keeps the agent's own policy, custom
    rules and all, moved to its directory.
    """

    roots: Literal["any"] | Sequence[str | Path] | None = "any"
    modes: Sequence[WorkspaceMode] = ("readonly", "coding")
    default_mode: WorkspaceMode | None = None
    build: Callable[[Path, WorkspaceMode], LocalWorkspace] | None = None
    agent_for: Callable[[Agent[Any], Path], Agent[Any]] | None = None
    general: str | Path | None = None
    remotes: Literal["any"] | Mapping[str, RemoteHost] | None = None
    agent_for_remote: Callable[[Agent[Any], WorkspaceRef], Agent[Any]] | None = None
    # The hosts built for ``remotes="any"``, one per destination for the
    # app's life: a host is a connection, and every chat there shares it.
    _hosts: dict[str, RemoteHost] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if self.roots is not None and self.roots != "any":
            if isinstance(self.roots, (str, Path)):
                raise UserError(
                    f"WorkspaceSwitch.roots must be 'any', a list or None, "
                    f"got {self.roots!r}",
                    hint="wrap a single directory in a list: roots=[...]",
                )
            object.__setattr__(
                self, "roots", tuple(resolve_root(r) for r in self.roots)
            )
        if self.general is not None:
            object.__setattr__(self, "general", resolve_root(self.general))
        unknown = [m for m in self.modes if m not in MODE_ORDER]
        if unknown:
            raise UserError(
                f"unknown workspace modes: {', '.join(map(repr, unknown))}",
                hint=f"choose from: {', '.join(MODE_ORDER)}",
            )
        if "readonly" not in self.modes:
            raise UserError(
                "WorkspaceSwitch.modes must include 'readonly'",
                hint="it is the floor a mode this server doesn't offer caps to",
            )
        ordered = tuple(m for m in MODE_ORDER if m in self.modes)
        object.__setattr__(self, "modes", ordered)
        if self.default_mode is not None and self.default_mode not in ordered:
            raise UserError(
                f"default_mode {self.default_mode!r} is not one of the offered modes"
            )

    def effective(self, mode: str) -> WorkspaceMode:
        """``mode`` as this server runs it: itself when offered, else the
        widest offered mode below it."""
        if mode in self.modes:
            return cast(WorkspaceMode, mode)
        rank = MODE_ORDER.index(mode) if mode in MODE_ORDER else 0
        below = [m for m in self.modes if MODE_ORDER.index(m) <= rank]
        return below[-1]

    def refusal(self, root: Path, own: Path | None) -> str | None:
        """Why a chat may not work in ``root`` (resolved), or ``None``.

        ``own`` is the agent's own root, always allowed, as is ``general``.
        """
        if root == own or root == self.general or self.roots == "any":
            return None
        if self.roots is None:
            return f"this server keeps every chat in {own}"
        roots = cast("tuple[Path, ...]", self.roots)
        if any(root.is_relative_to(r) for r in roots):
            return None
        listed = ", ".join(str(r) for r in roots) or "(none)"
        return f"only directories under {listed} are allowed"

    def workspace(
        self, base: LocalWorkspace, root: Path, mode: WorkspaceMode | None
    ) -> LocalWorkspace:
        """The agent's workspace ``base`` rebuilt for a chat in ``root`` with
        ``mode`` — ``None`` keeps ``base``'s own policy, moved to ``root``."""
        if mode is None:
            return replace(base, root=str(root))
        if self.build is not None:
            return self.build(root, mode)
        return Workspace.local(
            str(root),
            mode=mode,
            env=base.env,
            shell_timeout=base.shell_timeout,
            limits=base.limits,
            inherit_env=base.inherit_env,
            executor=base.executor,
        )

    # ---- remote locations ------------------------------------------------

    def remote_refusal(self, ref: WorkspaceRef) -> str | None:
        """Why a chat may not work at the remote ``ref``, or ``None``."""
        if self.remotes is None:
            return "this server doesn't open remote workspaces"
        declared = () if self.remotes == "any" else tuple(self.remotes)
        if ref.scheme == "remote":
            if ref.host in declared:
                return None
            listed = ", ".join(declared) or "(none)"
            return f"no remote host named {ref.host!r} is configured (there: {listed})"
        if self.remotes != "any":
            return "only the configured remote hosts are allowed here"
        return None

    def remote_host(self, ref: WorkspaceRef) -> RemoteHost:
        """The host behind an admitted ``ref``. Raises :class:`UserError` for
        a destination or container name that is not one safe word."""
        if ref.scheme == "remote":
            return cast("Mapping[str, RemoteHost]", self.remotes)[ref.host or ""]
        name = ref.host_name or ""
        host = self._hosts.get(name)
        if host is None:
            if ref.scheme == "ssh":
                host = RemoteHost.ssh(ref.host or "", name=name)
            else:
                user, _, container = (ref.host or "").rpartition("@")
                host = RemoteHost.docker(container, user=user or None, name=name)
            self._hosts[name] = host
        return host

    def remote_workspace(
        self, base: LocalWorkspace, ref: WorkspaceRef, mode: WorkspaceMode | None
    ) -> RemoteWorkspace:
        """The agent's workspace ``base`` as a chat at the remote ``ref``
        gets it: the policy :meth:`workspace` would give a local chat in
        ``mode`` — enforced over there — with ``base``'s limits and shell
        timeout. Not its ``env``: that was written for this machine.

        Raises :class:`UserError` for a workspace with a shell ``executor``:
        it confines commands on this machine and cannot go to the host, and
        leaving it behind would run them there unconfined.
        """
        # Built for the agent's own root (a path ``build`` may check exists
        # here) and never opened: only what does not depend on the root is
        # taken from it.
        template = self.workspace(base, resolve_root(base.root), mode)
        if template.executor is not None:
            raise UserError(
                "A workspace with a shell executor cannot be opened on a remote host.",
                hint=(
                    "the executor sandboxes commands on this machine only; on "
                    "the host they would run as its account, unconfined — "
                    "serve remote chats from an agent without one"
                ),
            )
        return RemoteWorkspace(
            host=self.remote_host(ref),
            root=ref.root,
            policy=template.policy,
            shell_timeout=template.shell_timeout,
            limits=template.limits,
        )

    async def aclose(self) -> None:
        """Close the hosts this switch built (``remotes="any"``). Declared
        hosts are their owner's to close."""
        hosts = list(self._hosts.values())
        self._hosts.clear()
        for host in hosts:
            try:
                await host.aclose()
            except Exception as exc:  # noqa: BLE001 — shutdown goes on
                log.warning("closing remote host %s failed: %s", host.name, exc)


def _only_policy_differs(old: WorkspaceLike, new: WorkspaceLike) -> bool:
    """Whether ``new`` is ``old`` with another ``policy`` and nothing else —
    answerable for dataclass configs with a ``policy`` field
    (``LocalWorkspace`` has one), False for anything else."""
    if type(old) is not type(new) or not is_dataclass(old):
        return False
    # A field ``replace`` can set: a derived ``policy`` (a property, an
    # ``init=False`` field) has no "same config, other policy" to compare.
    if not any(f.name == "policy" and f.init for f in fields(old)):
        return False
    return bool(replace(cast(Any, old), policy=cast(Any, new).policy) == new)


async def _close_quietly(session_id: str, session: WorkspaceSession) -> None:
    """Best-effort close: teardown on delete/shutdown paths must not mask
    the operation the user actually asked for."""
    try:
        await session.close()
    except Exception as exc:  # noqa: BLE001 — defensive teardown
        log.warning("closing workspace session for %s failed: %s", session_id, exc)


@dataclass
class WorkspaceSessions:
    """One live workspace session per web session id (chat, task, ...).

    Uniform across run sources — user chats, scheduled runs, and subagent
    task sessions each get their own entry keyed by their session id, and
    every deletion path funnels through :meth:`close`. Per-process, like the
    supervisor: under multiple uvicorn workers each process holds its own
    sessions.
    """

    _live: dict[str, tuple[WorkspaceLike, WorkspaceSession]] = field(
        default_factory=dict
    )
    # Serializes opens so two concurrent runs of one chat share a session
    # instead of racing two into existence (the loser would leak unclosed).
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def bind(self, session_id: str, agent: Agent[Any]) -> Agent[Any]:
        """Return ``agent`` with its workspace bound to the chat's session.

        Any backend whose sessions the runner would open and close per run
        (``close_after_run``) is intercepted — ``LocalWorkspace`` or a custom
        ``WorkspaceLike`` alike. No workspace passes through untouched, and
        so does an agent the caller already bound to a session of its own:
        that lifetime is not this registry's to manage.
        """
        cfg = agent.workspace
        if cfg is None or not cfg.close_after_run:
            return agent
        async with self._lock:
            held = self._live.get(session_id)
            if held is not None and getattr(held[1], "closed", False):
                # The session ended under us — a remote one whose connection
                # dropped, taking its processes along. This run starts over.
                await _close_quietly(session_id, held[1])
                held = None
            if held is not None and not (held[0] is cfg or held[0] == cfg):
                set_policy = getattr(held[1], "set_policy", None)
                if set_policy is not None and _only_policy_differs(held[0], cfg):
                    # Only the permissions changed (the chat switched mode):
                    # keep the session — and the dev server it runs — and
                    # judge from now on by the new policy.
                    set_policy(cast(Any, cfg).policy)
                    held = (cfg, held[1])
                    self._live[session_id] = held
                else:
                    # A different workspace (root, env, ...): the old session
                    # must not keep serving — close it (killing its
                    # background processes) and reopen.
                    await _close_quietly(session_id, held[1])
                    held = None
            if held is None:
                held = (cfg, await cfg.open())
                self._live[session_id] = held
        return replace(agent, workspace=WorkspaceSessionBinding(cfg, held[1]))

    def get(self, session_id: str) -> WorkspaceSession | None:
        """The chat's live session, or None (no run bound one yet)."""
        held = self._live.get(session_id)
        return held[1] if held is not None else None

    async def close(self, session_id: str) -> None:
        """Close and drop the chat's session (reaps its background processes).

        Idempotent; a later :meth:`bind` simply opens a fresh session.
        """
        async with self._lock:
            held = self._live.pop(session_id, None)
        if held is not None:
            await _close_quietly(session_id, held[1])

    async def aclose(self) -> None:
        """Close every live session (app shutdown)."""
        async with self._lock:
            live, self._live = self._live, {}
        for session_id, (_, session) in live.items():
            await _close_quietly(session_id, session)
