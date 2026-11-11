"""The decoupled JSON + SSE API for lovia agents.

:func:`build_api_router` returns a UI-free :class:`fastapi.APIRouter` you can
mount into your own FastAPI app to build a custom front-end::

    from fastapi import FastAPI
    from lovia.web import RouterDeps, build_api_router, ChatStore

    deps = RouterDeps(agents={"bot": agent}, store=ChatStore.in_memory())
    app = FastAPI(lifespan=deps.lifespan)
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

from ..schemas import ServerInfo, WorkspaceModesInfo
from .agents import build_agents_router
from .chat import build_chat_router
from .deps import RouterDeps
from .events import build_events_router
from .memory import build_memory_router, memory_plugin
from .schedules import build_schedules_router
from .sessions import build_sessions_router
from ..workspaces import files_config
from .workspace import build_workspace_router
from .workspaces import build_workspaces_router

__all__ = ["RouterDeps", "build_api_router"]


def _lovia_version() -> str | None:
    try:
        return _pkg_version("lovia")
    except PackageNotFoundError:  # pragma: no cover - source checkout w/o metadata
        return None


def _workspace_modes(deps: RouterDeps) -> WorkspaceModesInfo | None:
    switch = deps.workspace_switch
    if switch is None:
        return None
    default: str | None = switch.default_mode
    runtime = deps.config_runtime
    if runtime is not None:
        # The CLI's default follows Settings → Permissions, as this server
        # runs it.
        default = runtime.deployment.effective_mode(
            runtime.config.workspace.default_mode
        )
    if default is not None:
        default = switch.effective(default)
    # A chat with no mode stored runs on the agent's own workspace. The CLI
    # builds that one in the default mode, so it can say which; an embedder's
    # may carry a custom policy no mode name describes.
    own = default if runtime is not None else None
    return WorkspaceModesInfo(modes=list(switch.modes), default=default, own=own)


def build_api_router(deps: RouterDeps) -> APIRouter:
    """Assemble the complete JSON + SSE API (no HTML UI)."""
    router = APIRouter()

    @router.get("/api/info", response_model=ServerInfo)
    async def info() -> ServerInfo:
        return ServerInfo(
            title=deps.title,
            agents=list(deps.agents),
            default_agent=deps.default_agent,
            version=_lovia_version(),
            # False only in the CLI's not-yet-configured state (no served
            # agents): the UI then shows the first-run setup instead of chat.
            configured=bool(deps.agents),
            features={
                "checkpointing": deps.store.checkpointer is not None,
                "titles": deps.titles,
                # Truthy for both `followups=True` and a custom FollowupFn, so
                # the UI only asks for chips on servers that can produce them.
                "followups": bool(deps.followups),
                "scheduling": True,
                "workspace": any(
                    files_config(a) is not None for a in deps.agents.values()
                ),
                "memory": any(
                    memory_plugin(a) is not None for a in deps.agents.values()
                ),
                # Edit-and-resend / regenerate need the store's off-protocol
                # rewind (both bundled stores have it; custom ones may not).
                "rewind": hasattr(deps.session, "rewind"),
                # The /api/events lifecycle stream (push instead of poll).
                "events": True,
                # /api/config exists: the model connection is editable from
                # the UI (the CLI's default agent only — never embedders).
                "model_config": deps.config_runtime is not None,
                # create_app(workspaces=): chats pick a mode, and a directory
                # from GET /api/workspaces (whose can_add says if any beyond
                # the agent's own may be added).
                "workspace_switch": deps.workspace_switch is not None,
            },
            workspace_modes=_workspace_modes(deps),
        )

    if deps.config_runtime is not None:
        from .config import build_config_router

        router.include_router(build_config_router(deps.config_runtime))
    router.include_router(build_agents_router(deps))
    router.include_router(build_chat_router(deps))
    router.include_router(build_events_router(deps))
    router.include_router(build_sessions_router(deps))
    router.include_router(build_schedules_router(deps))
    router.include_router(build_workspace_router(deps))
    router.include_router(build_workspaces_router(deps))
    router.include_router(build_memory_router(deps))
    return router
