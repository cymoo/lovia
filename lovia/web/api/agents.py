"""Agent introspection routes."""

from __future__ import annotations

from typing import Any

try:
    from fastapi import APIRouter, HTTPException
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ...providers.base import context_window as provider_context_window
from ..schemas import AgentInfo
from .deps import RouterDeps
from .memory import memory_plugin
from .workspace import workspace_cfg


def resolve_context_window(agent: Agent[Any], server_policy: Any) -> int | None:
    """The context window the UI's meter should assume, or None when unknown.

    Precedence mirrors what compaction actually uses: an explicit window on
    the server-level policy, then on the agent's own policy, then whatever
    the provider advertises for its model.
    """
    for policy in (server_policy, getattr(agent, "context_policy", None)):
        window = getattr(policy, "context_window", None)
        if window:
            return int(window)
    model = getattr(agent, "model", None)
    return provider_context_window(model) if model is not None else None


def agent_info(
    name: str, agent: Agent[Any], *, context_window: int | None = None
) -> AgentInfo:
    """Public, JSON-safe view of an agent (name, static instructions, tools)."""
    return AgentInfo(
        name=name,
        instructions=agent.instructions
        if isinstance(agent.instructions, str)
        else None,
        tools=[t.name for t in (agent.tools or [])],
        workspace=workspace_cfg(agent) is not None,
        memory=memory_plugin(agent) is not None,
        context_window=context_window,
    )


def build_agents_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    def info(name: str, agent: Agent[Any]) -> AgentInfo:
        return agent_info(
            name,
            agent,
            context_window=resolve_context_window(agent, deps.context_policy),
        )

    @router.get("/api/agents", response_model=list[AgentInfo])
    async def list_agents() -> list[AgentInfo]:
        return [info(name, agent) for name, agent in deps.agents.items()]

    @router.get("/api/agents/{name}", response_model=AgentInfo)
    async def get_agent(name: str) -> AgentInfo:
        agent = deps.agents.get(name)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"unknown agent {name!r}")
        return info(name, agent)

    return router
