"""The decoupled JSON + SSE API for lovia agents.

:func:`build_api_router` returns a UI-free :class:`fastapi.APIRouter` you can
mount into your own FastAPI app to build a custom front-end::

    from fastapi import FastAPI
    from lovia.web import RouterDeps, build_api_router, ChatStore
    from lovia.web.approvals import ApprovalRegistry

    deps = RouterDeps(
        agents={"bot": agent},
        store=ChatStore.in_memory(),
        approvals=ApprovalRegistry(),
    )
    app = FastAPI()
    app.include_router(build_api_router(deps))
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    from fastapi import APIRouter
except ImportError as exc:  # pragma: no cover - depends on optional env
    from .._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..schemas import ServerInfo
from .agents import build_agents_router
from .chat import build_chat_router
from .deps import RouterDeps
from .memory import build_memory_router, memory_plugin
from .schedules import build_schedules_router
from .sessions import build_sessions_router
from .workspace import build_workspace_router, workspace_cfg

__all__ = ["RouterDeps", "build_api_router"]


def _lovia_version() -> str | None:
    try:
        return _pkg_version("lovia")
    except PackageNotFoundError:  # pragma: no cover - source checkout w/o metadata
        return None


def build_api_router(deps: RouterDeps) -> APIRouter:
    """Assemble the complete JSON + SSE API (no HTML UI)."""
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/api/info", response_model=ServerInfo)
    async def info() -> ServerInfo:
        return ServerInfo(
            title=deps.title,
            agents=list(deps.agents),
            default_agent=deps.default_agent,
            version=_lovia_version(),
            features={
                "checkpointing": deps.store.checkpointer is not None,
                "titles": deps.generate_titles,
                "scheduling": True,
                "workspace": any(
                    workspace_cfg(a) is not None for a in deps.agents.values()
                ),
                "memory": any(
                    memory_plugin(a) is not None for a in deps.agents.values()
                ),
            },
        )

    router.include_router(build_agents_router(deps))
    router.include_router(build_chat_router(deps))
    router.include_router(build_sessions_router(deps))
    router.include_router(build_schedules_router(deps))
    router.include_router(build_workspace_router(deps))
    router.include_router(build_memory_router(deps))
    return router
