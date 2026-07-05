"""Memory routes: view and edit an agent's hot-tier Notes from the web UI.

The Memory plugin's Notes are the small, always-in-context fact list the agent
curates for itself (``plugins/memory``). They are the user's data as much as
the agent's — "what does my assistant remember about me" — so the UI gets a
read *and* a write: the editor loads the canonical ``- fact`` body and saves a
replacement through :meth:`Memory.replace_notes`, which applies the same
normalization/dedup policy and lock as every other Notes write. The cold tier
(archive) stays API-less: it's derived data with its own recall tool.
"""

from __future__ import annotations

from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Query
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ...plugins.memory import Memory
from ..schemas import MemoryNotes, MemoryUpdate
from .deps import RouterDeps


def memory_plugin(agent: Agent[Any]) -> Memory | None:
    """The agent's Memory plugin, or None when it has no editable Notes."""
    for plugin in agent.plugins or []:
        if isinstance(plugin, Memory):
            return plugin
    return None


def build_memory_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    def require_memory(agent_name: str | None) -> Memory:
        plugin = memory_plugin(deps.pick(agent_name))
        if plugin is None:
            raise HTTPException(status_code=404, detail="agent has no memory")
        return plugin

    def notes_out(plugin: Memory, body: str) -> MemoryNotes:
        return MemoryNotes(content=body, used=len(body), budget=plugin.notes_budget)

    @router.get("/api/memory", response_model=MemoryNotes)
    async def get_memory(agent: str | None = Query(None)) -> MemoryNotes:
        plugin = require_memory(agent)
        return notes_out(plugin, await plugin.notes_body())

    @router.put("/api/memory", response_model=MemoryNotes)
    async def put_memory(
        payload: MemoryUpdate, agent: str | None = Query(None)
    ) -> MemoryNotes:
        """Replace the Notes; returns the canonical form actually stored."""
        plugin = require_memory(agent)
        return notes_out(plugin, await plugin.replace_notes(payload.content))

    return router
