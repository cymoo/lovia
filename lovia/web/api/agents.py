"""Agent introspection routes."""

from __future__ import annotations

from typing import Any

try:
    from fastapi import APIRouter, HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ..schemas import AgentInfo
from .deps import RouterDeps
from .memory import memory_plugin
from .workspace import workspace_cfg


def agent_info(name: str, agent: Agent[Any]) -> AgentInfo:
    """Public, JSON-safe view of an agent (name, static instructions, tools)."""
    return AgentInfo(
        name=name,
        instructions=agent.instructions
        if isinstance(agent.instructions, str)
        else None,
        tools=[t.name for t in (agent.tools or [])],
        workspace=workspace_cfg(agent) is not None,
        memory=memory_plugin(agent) is not None,
    )


def build_agents_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/api/agents", response_model=list[AgentInfo])
    async def list_agents() -> list[AgentInfo]:
        return [agent_info(name, agent) for name, agent in deps.agents.items()]

    @router.get("/api/agents/{name}", response_model=AgentInfo)
    async def get_agent(name: str) -> AgentInfo:
        agent = deps.agents.get(name)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"unknown agent {name!r}")
        return agent_info(name, agent)

    return router
