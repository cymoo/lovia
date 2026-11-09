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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast, get_args

from ..agent import Agent
from ..exceptions import UserError
from ..workspace import LocalWorkspace, Workspace, WorkspaceMode
from ..workspace.local import LocalWorkspaceSession
from ..workspace.protocol import WorkspaceSession

log = logging.getLogger(__name__)

__all__ = ["WorkspaceSessions", "WorkspaceSwitch"]

# Narrowest first: a mode may only ever be capped toward the front.
MODE_ORDER: tuple[str, ...] = get_args(WorkspaceMode)


@dataclass(frozen=True)
class WorkspaceSwitch:
    """Let each chat pick its own permission mode: ``create_app(workspaces=)``.

    Without it the served agent's workspace is what every chat gets. With it
    a chat stores a mode — chosen in the composer, or with ``workspace_mode``
    on its first message — and each of its runs gets the agent's workspace
    rebuilt for that mode. The root stays the agent's.

    * ``modes`` — what a chat may pick; ``readonly`` is always among them.
      A stored mode this server doesn't offer (a deployment narrowed since)
      runs as the widest offered mode below it: only ever narrower.
    * ``default_mode`` — what the page preselects for a new chat; ``None``
      runs new chats on the agent's own workspace until one is picked.
    * ``build`` — ``(root, mode) -> LocalWorkspace``. The default is the
      ``Workspace.local`` preset for the mode, keeping the agent workspace's
      env, limits, executor, ``inherit_env`` and shell timeout — but not a
      custom policy's rules. Pass ``build`` to carry those across modes.

    A chat that never picked a mode stays on the agent's own workspace,
    custom policy and all.
    """

    modes: Sequence[WorkspaceMode] = ("readonly", "coding")
    default_mode: WorkspaceMode | None = None
    build: Callable[[Path, WorkspaceMode], LocalWorkspace] | None = None

    def __post_init__(self) -> None:
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

    def workspace(self, base: LocalWorkspace, mode: WorkspaceMode) -> LocalWorkspace:
        """The agent's workspace ``base`` rebuilt for ``mode``."""
        if self.build is not None:
            return self.build(Path(base.root), mode)
        return Workspace.local(
            base.root,
            mode=mode,
            env=base.env,
            shell_timeout=base.shell_timeout,
            limits=base.limits,
            inherit_env=base.inherit_env,
            executor=base.executor,
        )


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

    _live: dict[str, tuple[LocalWorkspace, WorkspaceSession]] = field(
        default_factory=dict
    )
    # Serializes opens so two concurrent runs of one chat share a session
    # instead of racing two into existence (the loser would leak unclosed).
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def bind(self, session_id: str, agent: Agent[Any]) -> Agent[Any]:
        """Return ``agent`` with its workspace bound to the chat's session.

        Only plain :class:`LocalWorkspace` configs are intercepted. ``None``
        and custom ``WorkspaceLike`` backends pass through untouched, and so
        does an agent the caller already bound to a session of its own —
        their lifetimes are not this registry's to manage.
        """
        cfg = agent.workspace
        if not isinstance(cfg, LocalWorkspace):
            return agent
        async with self._lock:
            held = self._live.get(session_id)
            if held is not None and not (held[0] is cfg or held[0] == cfg):
                if replace(held[0], policy=cfg.policy) == cfg and isinstance(
                    held[1], LocalWorkspaceSession
                ):
                    # Only the permissions changed (the chat switched mode):
                    # keep the session — and the dev server it runs — and
                    # judge from now on by the new policy.
                    held[1].set_policy(cfg.policy)
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
        return replace(agent, workspace=cfg.bind(held[1]))

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
