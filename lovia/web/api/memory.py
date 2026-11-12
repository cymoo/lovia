"""Memory routes: view and edit an agent's hot-tier Notes from the web UI.

The Memory plugin's Notes are the small, always-in-context fact list the agent
curates for itself (``plugins/memory``). They are the user's data as much as
the agent's — "what does my assistant remember about me" — so the UI gets a
read *and* a write: the editor loads the canonical ``- fact`` body and saves a
replacement through :meth:`Memory.replace_notes`, which applies the same
normalization/dedup policy and lock as every other Notes write. The cold tier
(archive) stays API-less: it's derived data with its own recall tool — only
a project's can be dropped, with its Notes, by ``DELETE``.

Every route takes ``layer``: ``user`` (the default) or ``project``. A project
is named the way the workspace routes name a directory — ``session`` (that
chat's) or ``root`` (a draft's, checked like a new chat's), else the agent's
own — and keyed by :meth:`RouterDeps.project_of`, the rule a run's memory is
scoped by, so the editor and the agent always mean the same layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

try:
    from fastapi import APIRouter, Depends, Query
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ...exceptions import UserError
from ...plugins.memory import Memory
from ..errors import WebError
from ..run_deps import chat_project
from ..schemas import MemoryDreamResult, MemoryNotes, MemoryUpdate
from ..workspaces import ChatBinding
from .deps import RouterDeps

Layer = Literal["user", "project"]


def memory_plugin(agent: Agent[Any]) -> Memory | None:
    """The agent's Memory plugin, or None when it has no editable Notes."""
    for plugin in agent.plugins or []:
        if isinstance(plugin, Memory):
            return plugin
    return None


def splits_by_project(plugin: Memory) -> bool:
    """Whether ``plugin`` keeps a layer per chat project — the one scope the
    editor knows how to name (a custom ``scope=`` keys by something else)."""
    return plugin.scope is chat_project


@dataclass(frozen=True)
class _Layer:
    host: Agent[Any]
    plugin: Memory
    key: str | None  # None: the user's layer


def build_memory_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    async def target(
        agent: str | None = Query(None),
        layer: Layer = Query("user"),
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
    ) -> _Layer:
        meta = None
        if chat is not None:
            meta = await deps.store.get(chat)
            if meta is None:
                raise WebError(404, "session_not_found", "session not found")
            host = deps.pick(deps.agent_of(meta))
        else:
            host = deps.pick(agent)
        plugin = memory_plugin(host)
        if plugin is None:
            raise WebError(404, "feature_unavailable", "agent has no memory")
        if layer == "user":
            return _Layer(host, plugin, None)
        if not splits_by_project(plugin):
            raise WebError(
                404, "feature_unavailable", "agent's memory is not split by project"
            )
        binding: ChatBinding = (
            deps.chat_binding(host, meta)
            if meta is not None
            else deps.new_chat_binding(host, None, root)
        )
        key = deps.project_of(binding)
        if key is None:
            raise WebError(
                400,
                "invalid_request",
                "this chat belongs to no project",
                hint="chats in General, or without a workspace, keep only "
                "memory about you",
            )
        return _Layer(host, plugin, key)

    def notes_out(t: _Layer, body: str) -> MemoryNotes:
        return MemoryNotes(
            content=body,
            used=len(body),
            budget=t.plugin.notes_budget,
            dreamed_at=t.plugin.dreamed_at(scope=t.key),
            layer="user" if t.key is None else "project",
            scope_key=t.key,
        )

    @router.get("/api/memory", response_model=MemoryNotes)
    async def get_memory(t: _Layer = Depends(target)) -> MemoryNotes:
        return notes_out(t, await t.plugin.notes_body(scope=t.key))

    @router.put("/api/memory", response_model=MemoryNotes)
    async def put_memory(
        payload: MemoryUpdate, t: _Layer = Depends(target)
    ) -> MemoryNotes:
        """Replace the Notes; returns the canonical form actually stored."""
        return notes_out(t, await t.plugin.replace_notes(payload.content, scope=t.key))

    @router.delete("/api/memory", response_model=MemoryNotes)
    async def clear_memory(t: _Layer = Depends(target)) -> MemoryNotes:
        """Forget a project: its Notes and its archive, irreversibly.

        Only a project's layer — the user's is emptied in the editor, and
        has no clear (its archive is every General chat's history).
        """
        if t.key is None:
            raise WebError(
                400,
                "invalid_request",
                "only a project's memory can be cleared",
                hint="pass layer=project; edit your own notes in the editor",
            )
        await t.plugin.clear_scope(t.key)
        return notes_out(t, await t.plugin.notes_body(scope=t.key))

    @router.post("/api/memory/dream", response_model=MemoryDreamResult)
    async def dream_memory(t: _Layer = Depends(target)) -> MemoryDreamResult:
        """Run the dream pass now; returns counts plus the fresh body.

        The editor's tidy-up button. The plugin's own model wins when
        configured; otherwise the host agent's model runs the rewrite —
        same resolution curation uses at run end.
        """
        plugin = t.plugin
        try:
            before, after = await plugin.dream(
                model=plugin.model or t.host.model, scope=t.key
            )
        except UserError as exc:
            raise WebError.from_exc(400, "invalid_request", exc) from exc
        base = notes_out(t, await plugin.notes_body(scope=t.key))
        return MemoryDreamResult(**base.model_dump(), before=before, after=after)

    return router
