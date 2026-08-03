"""Configuration endpoints — the backend of the web UI's Settings.

Mounted only when the app serves the CLI's default agent (``create_app``
received a :class:`~lovia.web.config.ConfigRuntime`); embedders configure
their agents in code and these routes simply don't exist.

Secrets never round-trip: API keys are written through this API but read
back only as ``{"set": true, "hint": "sk-…9f2a"}``. Every successful write
re-applies the whole document through the runtime (validate by building →
persist → atomically swap the served agent), so what this API accepted is
always exactly what the next message runs on.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from ...exceptions import UserError
from ..config.probe import unlisted_model_note, validate_connection
from ..config.runtime import ConfigRuntime
from ..config.schema import (
    ANTHROPIC_FLAVOR,
    OPENAI_FLAVOR,
    Connection,
    FlavorName,
    ModelProfile,
    SearchBackend,
    VisionMode,
    WebConfig,
    slugify,
)
from ..config.storage import PROJECT_CONFIG_LABEL, USER_CONFIG_LABEL

# Loopback names a same-machine browser legitimately uses. Anything else in
# the Host header of an *unauthenticated* config write is a DNS-rebinding
# smell — a foreign page resolving its own domain to 127.0.0.1.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _mask(secret: str | None) -> dict[str, object]:
    from ..config.check import mask_key

    if not secret:
        return {"set": False, "hint": None}
    return {"set": True, "hint": mask_key(secret)}


class ProfileIn(BaseModel):
    """Create/update payload for one model profile.

    ``api_key`` semantics: ``None`` (or absent) keeps the stored key on
    update (means "no key" on create); an empty string clears it; any other
    string replaces it. The one field a GET can't echo needs an explicit
    keep-vs-clear convention.
    """

    id: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=64
    )
    name: str | None = None
    model: str = Field(min_length=1)
    flavor: FlavorName = "openai"
    base_url: str | None = None
    api_key: str | None = None
    context_window: int | None = Field(default=None, ge=1)
    vision: VisionMode = "auto"

    def to_profile(self, *, profile_id: str, stored_key: str | None) -> ModelProfile:
        if self.api_key is None:
            key = stored_key
        else:
            key = self.api_key or None  # "" clears
        return ModelProfile(
            id=profile_id,
            name=self.name,
            model=self.model,
            flavor=self.flavor,
            base_url=self.base_url,
            api_key=key,
            context_window=self.context_window,
            vision=self.vision,
        )


class RolesIn(BaseModel):
    """Role updates; only the fields present in the request change."""

    chat: str | None = None
    vision: str | None = None
    aux: str | None = None


class SearchIn(BaseModel):
    """Search updates; ``tavily_api_key`` follows the keep/clear convention."""

    backend: SearchBackend | None = None
    tavily_api_key: str | None = None


class TestIn(BaseModel):
    """A connection to probe: free-form fields, or a stored profile's.

    With ``profile_id`` set, absent fields fall back to the stored profile —
    in particular ``api_key: null`` reuses the stored key, so the UI can
    test without ever holding the secret. An explicit ``""`` means "probe
    without a key" and never falls back.
    """

    profile_id: str | None = None
    model: str | None = None
    flavor: FlavorName = "openai"
    base_url: str | None = None
    api_key: str | None = None
    context_window: int | None = None


class TestOut(BaseModel):
    outcome: Literal["ok", "auth_failed", "unreachable", "unverifiable"]
    detail: str
    context_window: int | None = None
    window_from_endpoint: bool = False
    models: list[str] | None = None
    note: str | None = None


def build_config_router(runtime: ConfigRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/config")

    def _guard_host(request: Request) -> None:
        """Refuse unauthenticated config writes with a non-local Host header.

        Cookie CSRF is already blunted (SameSite=Lax + JSON bodies force a
        preflight); this closes the DNS-rebinding hole a token-less loopback
        deployment would otherwise have. Deployments behind a proxy carry a
        token/auth, which makes the Host header irrelevant.
        """
        if not runtime.require_local_host:
            return
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host in _LOCAL_HOSTS or host.endswith(".localhost"):
            return
        raise HTTPException(
            status_code=403,
            detail="configuration changes require a local origin; "
            "set --token to configure through a proxy",
        )

    def _config_out() -> dict[str, object]:
        cfg = runtime.config
        return {
            "configured": runtime.configured,
            "missing": ConfigRuntime.serveable(cfg),
            "scope": {
                "label": runtime.loaded.label,
                "exists": runtime.loaded.exists,
                "project_label": PROJECT_CONFIG_LABEL,
                "user_label": USER_CONFIG_LABEL,
            },
            "defaults": {
                "openai_base_url": OPENAI_FLAVOR.default_base_url,
                "anthropic_base_url": ANTHROPIC_FLAVOR.default_base_url,
            },
            "models": [
                {
                    "id": p.id,
                    "name": p.name,
                    "display_name": p.display_name,
                    "model": p.model,
                    "flavor": p.flavor,
                    "base_url": p.base_url,
                    "api_key": _mask(p.api_key),
                    "context_window": p.context_window,
                    "vision": p.vision,
                }
                for p in cfg.models
            ],
            "roles": cfg.roles.model_dump(),
            "search": {
                "backend": cfg.search.backend,
                "tavily_api_key": _mask(cfg.search.tavily_api_key),
            },
        }

    async def _apply(config: WebConfig) -> None:
        """Run the swap; translate build failures into API errors."""
        try:
            await runtime.apply(config)
        except (UserError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _copy(**overrides: object) -> WebConfig:
        """A validated deep copy of the current document with edits applied."""
        cfg = runtime.config
        payload = cfg.model_dump()
        payload.update(overrides)
        try:
            return WebConfig.model_validate(payload)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ()))
            raise HTTPException(
                status_code=400, detail=f"{where}: {first.get('msg', 'invalid')}"
            ) from exc

    @router.get("")
    async def get_config(response: Response) -> dict[str, object]:
        # Masked keys still describe the secret's presence — never cache.
        response.headers["Cache-Control"] = "no-store"
        return _config_out()

    @router.post("/models", status_code=201)
    async def create_model(body: ProfileIn, request: Request) -> dict[str, object]:
        _guard_host(request)
        cfg = runtime.config
        taken = {p.id for p in cfg.models}
        profile_id = body.id or slugify(body.name or body.model, taken)
        if profile_id in taken:
            raise HTTPException(
                status_code=409, detail=f"model id {profile_id!r} exists"
            )
        try:
            profile = body.to_profile(profile_id=profile_id, stored_key=None)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        models = [p.model_dump() for p in cfg.models] + [profile.model_dump()]
        roles = cfg.roles.model_dump()
        if roles.get("chat") is None:
            roles["chat"] = profile_id  # the first profile becomes the default
        await _apply(_copy(models=models, roles=roles))
        return {"id": profile_id, "config": _config_out()}

    @router.put("/models/{profile_id}")
    async def update_model(
        profile_id: str, body: ProfileIn, request: Request
    ) -> dict[str, object]:
        _guard_host(request)
        cfg = runtime.config
        stored = cfg.profile(profile_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown model {profile_id!r}")
        try:
            profile = body.to_profile(profile_id=profile_id, stored_key=stored.api_key)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        models = [
            profile.model_dump() if p.id == profile_id else p.model_dump()
            for p in cfg.models
        ]
        await _apply(_copy(models=models))
        return {"id": profile_id, "config": _config_out()}

    @router.delete("/models/{profile_id}")
    async def delete_model(profile_id: str, request: Request) -> dict[str, object]:
        _guard_host(request)
        cfg = runtime.config
        if cfg.profile(profile_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown model {profile_id!r}")
        if cfg.roles.chat == profile_id:
            raise HTTPException(
                status_code=409,
                detail="this is the default chat model; make another model the "
                "default first",
            )
        models = [p.model_dump() for p in cfg.models if p.id != profile_id]
        roles = {
            role: (None if ref == profile_id else ref)
            for role, ref in cfg.roles.model_dump().items()
        }
        await _apply(_copy(models=models, roles=roles))
        return {"config": _config_out()}

    @router.put("/roles")
    async def update_roles(body: RolesIn, request: Request) -> dict[str, object]:
        _guard_host(request)
        roles = runtime.config.roles.model_dump()
        # Only the fields the client sent change; explicit null clears
        # (vision/aux) or is rejected (chat must always point somewhere).
        for role in body.model_fields_set:
            roles[role] = getattr(body, role)
        if roles.get("chat") is None:
            raise HTTPException(status_code=400, detail="roles.chat cannot be cleared")
        await _apply(_copy(roles=roles))
        return {"config": _config_out()}

    @router.put("/search")
    async def update_search(body: SearchIn, request: Request) -> dict[str, object]:
        _guard_host(request)
        search = runtime.config.search.model_dump()
        if body.backend is not None:
            search["backend"] = body.backend
        if body.tavily_api_key is not None:
            search["tavily_api_key"] = body.tavily_api_key or None  # "" clears
        await _apply(_copy(search=search))
        return {"config": _config_out()}

    @router.post("/test")
    async def test_connection(body: TestIn, request: Request) -> TestOut:
        # Read-only against the config, but it *sends the key* to the given
        # base URL — treat it as a write for the rebinding guard.
        _guard_host(request)
        stored = runtime.config.profile(body.profile_id) if body.profile_id else None
        if body.profile_id is not None and stored is None:
            raise HTTPException(
                status_code=404, detail=f"unknown model {body.profile_id!r}"
            )
        model = body.model or (stored.model if stored else None)
        if not model:
            raise HTTPException(status_code=400, detail="model is required")
        # Key selection mirrors the write endpoints' keep/clear convention:
        # None falls back to the stored profile's key, while an explicit ""
        # means "probe keyless" — it must never silently substitute the
        # stored secret (the base URL is caller-chosen).
        if body.api_key is not None:
            probe_key = body.api_key or None
        else:
            probe_key = stored.api_key if stored else None
        try:
            probe_profile = ModelProfile(
                id="probe",
                model=model,
                flavor=body.flavor
                if body.model
                else (stored.flavor if stored else body.flavor),
                base_url=body.base_url or (stored.base_url if stored else None),
                api_key=probe_key,
                context_window=body.context_window,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        conn = Connection.from_profile(probe_profile)
        # The probe is synchronous httpx (shared with the terminal wizard);
        # keep the event loop free while it waits on the network.
        outcome, detail = await asyncio.to_thread(validate_connection, conn)
        return TestOut(
            outcome=outcome.value,
            detail=detail,
            context_window=conn.context_window,
            window_from_endpoint=conn.window_from_endpoint,
            models=conn.available_models,
            note=unlisted_model_note(conn),
        )

    return router
