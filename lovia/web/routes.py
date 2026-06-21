"""Backward-compatible single-router factory.

Superseded by the split :mod:`lovia.web.api` subpackage plus :mod:`lovia.web.ui`.
Kept so existing callers of ``build_router`` keep working; new code should prefer
``build_api_router`` (UI-free) and ``build_ui_router`` directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

try:
    from fastapi import APIRouter
except ImportError as exc:  # pragma: no cover - depends on optional env
    from ._deps import raise_missing_web_extra

    raise_missing_web_extra(exc)

from ..agent import Agent
from ..context import ContextPolicy
from ..providers import Provider
from ..reliability import RetryPolicy, RunBudget
from ..tracing import Tracer
from .api import RouterDeps, build_api_router
from .approvals import ApprovalRegistry
from .store import ChatStore
from .ui import build_ui_router


def build_router(
    agents: dict[str, Agent[Any]],
    store: ChatStore,
    approvals: ApprovalRegistry,
    *,
    context_policy: ContextPolicy | None = None,
    title_model: str | Provider | list[str | Provider] | None = None,
    generate_titles: bool = True,
    title: str = "lovia",
    max_turns: int = 50,
    budget: RunBudget | None = None,
    retry: RetryPolicy | None = None,
    tracer: Tracer | None = None,
    empty_title: str = "Wake up, Neo.",
    empty_description: str | Sequence[str] | None = None,
) -> APIRouter:
    """Build the combined API + bundled-UI router (deprecated; see module docs)."""
    deps = RouterDeps(
        agents=agents,
        store=store,
        approvals=approvals,
        title=title,
        context_policy=context_policy,
        title_model=title_model,
        generate_titles=generate_titles,
        max_turns=max_turns,
        budget=budget,
        retry=retry,
        tracer=tracer,
    )
    router = APIRouter()
    router.include_router(build_api_router(deps))
    router.include_router(
        build_ui_router(
            title=title,
            empty_title=empty_title,
            empty_description=empty_description,
        )
    )
    return router
