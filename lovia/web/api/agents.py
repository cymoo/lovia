"""Agent introspection routes."""

from __future__ import annotations

from typing import Any

try:
    from fastapi import APIRouter, Query
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ...agent import Agent
from ...plugins.skills import Skills
from ...providers.base import context_window as provider_context_window
from ..errors import WebError
from ..schemas import AgentInfo, SkillInfo
from .deps import RouterDeps
from .memory import memory_plugin, splits_by_project
from ..workspaces import files_config


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


def model_name(agent: Agent[Any]) -> str | None:
    """The model id the agent runs on, or None when it can't be named.

    ``agent.model`` is either the id string itself or a Provider instance —
    the bundled providers all keep the id on a ``model`` attribute.
    """
    model = getattr(agent, "model", None)
    if isinstance(model, str):
        return model
    name = getattr(model, "model", None)
    return name if isinstance(name, str) else None


def agent_info(
    name: str, agent: Agent[Any], *, context_window: int | None = None
) -> AgentInfo:
    """Public, JSON-safe view of an agent (name, static instructions, tools)."""
    memory = memory_plugin(agent)
    return AgentInfo(
        name=name,
        instructions=agent.instructions
        if isinstance(agent.instructions, str)
        else None,
        tools=[t.name for t in (agent.tools or [])],
        workspace=files_config(agent) is not None,
        memory=memory is not None,
        memory_projects=memory is not None and splits_by_project(memory),
        context_window=context_window,
        model=model_name(agent),
    )


def build_agents_router(deps: RouterDeps) -> APIRouter:
    router = APIRouter()

    def info(name: str, agent: Agent[Any]) -> AgentInfo:
        out = agent_info(
            name,
            agent,
            context_window=resolve_context_window(agent, deps.context_policy),
        )
        runtime = deps.config_runtime
        spec = runtime.agent_config(name) if runtime is not None else None
        if runtime is None or spec is None:
            return out
        mode = runtime.deployment.effective_mode(spec.mode)
        switch = deps.workspace_switch
        if mode is not None and switch is not None:
            mode = switch.effective(mode)
        return out.model_copy(
            update={
                "label": spec.display_name,
                "mode": mode,
            }
        )

    @router.get("/api/agents", response_model=list[AgentInfo])
    async def list_agents() -> list[AgentInfo]:
        return [info(name, agent) for name, agent in deps.agents.items()]

    @router.get("/api/agents/{name}", response_model=AgentInfo)
    async def get_agent(name: str) -> AgentInfo:
        agent = deps.agents.get(name)
        if agent is None:
            raise WebError(404, "agent_not_found", f"unknown agent {name!r}")
        return info(name, agent)

    @router.get("/api/skills", response_model=list[SkillInfo])
    async def list_skills(
        agent: str | None = Query(None),
        chat: str | None = Query(None, alias="session"),
        root: str | None = Query(None, max_length=4096),
    ) -> list[SkillInfo]:
        """The skills a run of chat ``session`` — or of a draft opening in
        ``root`` with ``agent`` — sees: its agent built the way the run
        builds it (``effective_agent``, so the project's skills, a remote
        chat's lack of them, and the agent's own settings all apply), then
        each Skills plugin's catalog, filter and precedence included."""
        if chat is not None:
            meta = await deps.store.get(chat)
            if meta is None:
                raise WebError(404, "session_not_found", "session not found")
            host = deps.pick(deps.agent_of(meta))
            binding = deps.chat_binding(host, meta)
        else:
            host = deps.pick(agent)
            binding = deps.new_chat_binding(host, None, root)
        return [
            SkillInfo(name=m.name, description=m.description)
            for plugin in deps.effective_agent(host, binding).plugins
            if isinstance(plugin, Skills)
            for m in await plugin.catalog.list_skills()
        ]

    return router
