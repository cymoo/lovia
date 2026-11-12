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
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, ValidationError

from ...exceptions import UserError
from ...reliability import RetryPolicy
from ..config.probe import unlisted_model_note, validate_connection
from ..config.runtime import ConfigRuntime
from ..config.schema import (
    ANTHROPIC_FLAVOR,
    GLOBAL_SKILLS_HINT,
    OPENAI_FLAVOR,
    USER_SKILLS_DIR,
    AgentConfig,
    AutoAllowBundle,
    Connection,
    FlavorName,
    ModelProfile,
    SearchBackend,
    VisionMode,
    WebConfig,
    WorkspaceModeName,
    agent_slug,
    is_global_dir,
    slugify,
)
from ..config.skills import scan_skills_status
from ..errors import WebError

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
    max_tokens: int | None = Field(default=None, ge=1)
    vision: VisionMode = "auto"
    extra_body: dict[str, Any] = Field(default_factory=dict)

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
            max_tokens=self.max_tokens,
            vision=self.vision,
            extra_body=self.extra_body,
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


class SkillsIn(BaseModel):
    """Full-list replacement for the global skill directories (order =
    precedence). A relative entry is refused here, even the project
    convention a saved file may still carry: adding one could only be a
    misunderstanding, since a project's skills load by themselves."""

    dirs: list[str]


class AgentIn(BaseModel):
    """An agent's settings, as created or edited; only the fields present
    change. ``null`` means "inherit" where a field has that meaning —
    ``name`` (show the id), ``model`` (the default chat model), ``skills``
    (the global directories), ``max_retries`` (the core default) — and is
    refused elsewhere. The id is never written: it is derived from the
    name on creation and fixed after."""

    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    model: str | None = None
    mode: WorkspaceModeName | None = None
    auto_allow: list[AutoAllowBundle] | None = None
    skills: list[str] | None = None
    search: bool | None = None
    memory: bool | None = None
    subagents: bool | None = None
    max_turns: int | None = Field(default=None, ge=1)
    max_retries: int | None = Field(default=None, ge=0)
    followups: bool | None = None


# What an explicit null means "inherit" for; anywhere else it is refused.
_INHERITABLE = frozenset({"name", "model", "skills", "max_retries"})
# Fixed by --readonly / --no-workspace: the browser can't change them then.
_PERMISSION_FIELDS = frozenset({"mode", "auto_allow"})


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


def _refuse_relative(dirs: list[str]) -> None:
    """A written skill directory must be global — even the project entry a
    saved file may still carry: adding one could only be a misunderstanding,
    since a project's skills load by themselves."""
    relative = [d.strip() for d in dirs if d.strip() and not is_global_dir(d.strip())]
    if relative:
        raise WebError(
            400,
            "invalid_request",
            f"{relative[0]!r} is not a global path",
            hint=GLOBAL_SKILLS_HINT,
        )


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
        raise WebError(
            403,
            "local_origin_required",
            "configuration changes require a local origin",
            hint="set --token to configure through a proxy",
        )

    def _guard_write(request: Request) -> None:
        """Every config write: the rebinding guard, then a writable file.

        A read-only ``config.json`` (a read-only mount, say) refuses up
        front rather than after the swap — Settings shows it read-only too.
        """
        _guard_host(request)
        if not runtime.writable:
            raise WebError(
                409,
                "config_read_only",
                f"{runtime.loaded.label} is not writable",
                hint="edit the file directly, then restart lovia web",
            )

    def _permissions_out() -> dict[str, object]:
        from ..builder import AUTO_ALLOW_BUNDLES

        deployment = runtime.deployment
        return {
            "modes": list(deployment.modes()),
            "bundles": list(AUTO_ALLOW_BUNDLES),
            # None, or why the browser can't change an agent's permissions.
            "lock": deployment.workspace_lock(),
        }

    def _agent_out(agent: AgentConfig) -> dict[str, object]:
        return {
            **agent.model_dump(),
            "display_name": agent.display_name,
            # What its runs actually get, once the deployment had its say.
            "effective_mode": runtime.deployment.effective_mode(agent.mode),
        }

    def _config_out() -> dict[str, object]:
        cfg = runtime.config
        return {
            "configured": runtime.configured,
            "missing": ConfigRuntime.serveable(cfg),
            "writable": runtime.writable,
            "scope": {
                "label": runtime.loaded.label,
                "exists": runtime.loaded.exists,
            },
            "defaults": {
                "openai_base_url": OPENAI_FLAVOR.default_base_url,
                "anthropic_base_url": ANTHROPIC_FLAVOR.default_base_url,
                # Retries when an agent's max_retries is unset: the core
                # RetryPolicy default.
                "max_retries": RetryPolicy().max_attempts - 1,
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
                    "max_tokens": p.max_tokens,
                    "vision": p.vision,
                    "extra_body": p.extra_body,
                }
                for p in cfg.models
            ],
            "roles": cfg.roles.model_dump(),
            "search": {
                "backend": cfg.search.backend,
                "tavily_api_key": _mask(cfg.search.tavily_api_key),
            },
            "skills": cfg.skills.model_dump(),
            # The first is the default agent.
            "agents": [_agent_out(a) for a in cfg.agents],
            "permissions": _permissions_out(),
        }

    async def _apply(config: WebConfig, *, settings_only: bool = False) -> None:
        """Run the swap; translate build failures into API errors."""
        try:
            await runtime.apply(config, settings_only=settings_only)
        except (UserError, ValueError) as exc:
            raise WebError.from_exc(400, "invalid_request", exc) from exc

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
            raise WebError(
                400, "invalid_request", f"{where}: {first.get('msg', 'invalid')}"
            ) from exc

    @router.get("")
    async def get_config(response: Response) -> dict[str, object]:
        # Masked keys still describe the secret's presence — never cache.
        response.headers["Cache-Control"] = "no-store"
        return _config_out()

    @router.post("/models", status_code=201)
    async def create_model(body: ProfileIn, request: Request) -> dict[str, object]:
        _guard_write(request)
        cfg = runtime.config
        taken = {p.id for p in cfg.models}
        profile_id = body.id or slugify(body.name or body.model, taken)
        if profile_id in taken:
            raise WebError(409, "model_exists", f"model id {profile_id!r} exists")
        try:
            profile = body.to_profile(profile_id=profile_id, stored_key=None)
        except ValidationError as exc:
            raise WebError.from_exc(400, "invalid_request", exc) from exc
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
        _guard_write(request)
        cfg = runtime.config
        stored = cfg.profile(profile_id)
        if stored is None:
            raise WebError(404, "model_not_found", f"unknown model {profile_id!r}")
        try:
            profile = body.to_profile(profile_id=profile_id, stored_key=stored.api_key)
        except ValidationError as exc:
            raise WebError.from_exc(400, "invalid_request", exc) from exc
        models = [
            profile.model_dump() if p.id == profile_id else p.model_dump()
            for p in cfg.models
        ]
        await _apply(_copy(models=models))
        return {"id": profile_id, "config": _config_out()}

    @router.post("/models/{profile_id}/duplicate", status_code=201)
    async def duplicate_model(profile_id: str, request: Request) -> dict[str, object]:
        """A server-side copy, API key included — the key never reaches the
        browser, so a variant of the same endpoint (say, thinking off for the
        aux role) can't be made by copying the form."""
        _guard_write(request)
        cfg = runtime.config
        stored = cfg.profile(profile_id)
        if stored is None:
            raise WebError(404, "model_not_found", f"unknown model {profile_id!r}")
        taken = {p.id for p in cfg.models}
        copy_id = slugify(f"{profile_id}-copy", taken)
        copy = stored.model_copy(
            update={"id": copy_id, "name": f"{stored.display_name} copy"}
        )
        models = [p.model_dump() for p in cfg.models] + [copy.model_dump()]
        await _apply(_copy(models=models))
        return {"id": copy_id, "config": _config_out()}

    @router.delete("/models/{profile_id}")
    async def delete_model(profile_id: str, request: Request) -> dict[str, object]:
        _guard_write(request)
        cfg = runtime.config
        if cfg.profile(profile_id) is None:
            raise WebError(404, "model_not_found", f"unknown model {profile_id!r}")
        if cfg.roles.chat == profile_id:
            raise WebError(
                409,
                "model_in_use",
                "this is the default chat model",
                hint="make another model the default first",
            )
        models = [p.model_dump() for p in cfg.models if p.id != profile_id]
        roles = {
            role: (None if ref == profile_id else ref)
            for role, ref in cfg.roles.model_dump().items()
        }
        # An agent on it follows the default chat model from now on.
        agents = [
            {**a.model_dump(), "model": None if a.model == profile_id else a.model}
            for a in cfg.agents
        ]
        await _apply(_copy(models=models, roles=roles, agents=agents))
        return {"config": _config_out()}

    @router.put("/roles")
    async def update_roles(body: RolesIn, request: Request) -> dict[str, object]:
        _guard_write(request)
        roles = runtime.config.roles.model_dump()
        # Only the fields the client sent change; explicit null clears
        # (vision/aux) or is rejected (chat must always point somewhere).
        for role in body.model_fields_set:
            roles[role] = getattr(body, role)
        if roles.get("chat") is None:
            raise WebError(400, "invalid_request", "roles.chat cannot be cleared")
        await _apply(_copy(roles=roles))
        return {"config": _config_out()}

    @router.put("/search")
    async def update_search(body: SearchIn, request: Request) -> dict[str, object]:
        _guard_write(request)
        search = runtime.config.search.model_dump()
        if body.backend is not None:
            search["backend"] = body.backend
        if body.tavily_api_key is not None:
            search["tavily_api_key"] = body.tavily_api_key or None  # "" clears
        await _apply(_copy(search=search))
        return {"config": _config_out()}

    @router.put("/skills")
    async def update_skills(body: SkillsIn, request: Request) -> dict[str, object]:
        _guard_write(request)
        _refuse_relative(body.dirs)
        await _apply(_copy(skills={"dirs": body.dirs}))
        return {"config": _config_out()}

    # ---- agents ----------------------------------------------------------
    # Every agent write is ``settings_only``: an agent names no new model
    # (only one already configured), so before any is, it is just saved.

    def _stored_agent(agent_id: str) -> AgentConfig:
        agent = runtime.config.agent(agent_id)
        if agent is None:
            raise WebError(404, "agent_not_found", f"unknown agent {agent_id!r}")
        return agent

    def _edited(stored: dict[str, Any], body: AgentIn) -> dict[str, Any]:
        """``stored`` with ``body``'s fields applied, checked against what
        this deployment lets the browser change."""
        deployment = runtime.deployment
        lock = deployment.workspace_lock()
        edited = dict(stored)
        for name in body.model_fields_set:
            value = getattr(body, name)
            if value is None and name not in _INHERITABLE:
                raise WebError(400, "invalid_request", f"{name} cannot be null")
            if value == stored.get(name):
                continue  # a whole-form save sends back what it didn't touch
            if name in _PERMISSION_FIELDS and lock:
                raise WebError(
                    403,
                    "workspace_locked",
                    "permissions are fixed by how lovia web was started",
                    hint={
                        "no_workspace": "started with --no-workspace",
                        "readonly": "started with --readonly",
                    }[lock],
                )
            if name == "mode" and value not in deployment.modes():
                raise WebError(
                    400,
                    "invalid_request",
                    f"mode {value!r} is not available on this server",
                    hint=f"choose one of: {', '.join(deployment.modes())}",
                )
            if name == "skills" and value is not None:
                _refuse_relative(value)
            edited[name] = value
        return edited

    async def _apply_agents(agents: list[dict[str, Any]]) -> None:
        await _apply(_copy(agents=agents), settings_only=True)

    @router.post("/agents", status_code=201)
    async def create_agent(body: AgentIn, request: Request) -> dict[str, object]:
        _guard_write(request)
        cfg = runtime.config
        new_id = agent_slug(body.name or "", {a.id for a in cfg.agents})
        fields = _edited(AgentConfig(id=new_id).model_dump(), body)
        await _apply_agents([a.model_dump() for a in cfg.agents] + [fields])
        return {"id": new_id, "config": _config_out()}

    @router.put("/agents/{agent_id}")
    async def update_agent(
        agent_id: str, body: AgentIn, request: Request
    ) -> dict[str, object]:
        _guard_write(request)
        _stored_agent(agent_id)
        agents = [
            _edited(a.model_dump(), body) if a.id == agent_id else a.model_dump()
            for a in runtime.config.agents
        ]
        await _apply_agents(agents)
        return {"id": agent_id, "config": _config_out()}

    @router.post("/agents/{agent_id}/duplicate", status_code=201)
    async def duplicate_agent(agent_id: str, request: Request) -> dict[str, object]:
        _guard_write(request)
        cfg = runtime.config
        stored = _stored_agent(agent_id)
        copy = stored.model_copy(
            update={
                "id": agent_slug(f"{agent_id}-copy", {a.id for a in cfg.agents}),
                "name": f"{stored.display_name} copy",
            }
        )
        await _apply_agents([a.model_dump() for a in cfg.agents] + [copy.model_dump()])
        return {"id": copy.id, "config": _config_out()}

    @router.post("/agents/{agent_id}/default")
    async def make_default_agent(agent_id: str, request: Request) -> dict[str, object]:
        """Make it the default: the first in the list."""
        _guard_write(request)
        stored = _stored_agent(agent_id)
        rest = [a.model_dump() for a in runtime.config.agents if a.id != agent_id]
        await _apply_agents([stored.model_dump(), *rest])
        return {"config": _config_out()}

    @router.delete("/agents/{agent_id}")
    async def delete_agent(agent_id: str, request: Request) -> dict[str, object]:
        """Chats and schedules that ran on it run on the default from now on."""
        _guard_write(request)
        _stored_agent(agent_id)
        rest = [a.model_dump() for a in runtime.config.agents if a.id != agent_id]
        if not rest:
            raise WebError(
                409,
                "agent_in_use",
                "this is the only agent",
                hint="add another agent first",
            )
        await _apply_agents(rest)
        return {"config": _config_out()}

    @router.get("/skills")
    async def get_skills(response: Response) -> dict[str, object]:
        """Per-directory discovery report for the Settings pane.

        Live filesystem truth (a directory created after the last apply
        already shows ``exists``), so never cache; local paths are also
        not something an intermediary should hold on to.
        """
        response.headers["Cache-Control"] = "no-store"
        roots = await asyncio.to_thread(scan_skills_status, runtime.config.skills)
        return {
            # The conventional root, so the pane can offer restoring a
            # removed default without hardcoding the path client-side.
            "defaults": [USER_SKILLS_DIR],
            "roots": [asdict(r) for r in roots],
        }

    @router.post("/test")
    async def test_connection(body: TestIn, request: Request) -> TestOut:
        # Read-only against the config, but it *sends the key* to the given
        # base URL — treat it as a write for the rebinding guard.
        _guard_host(request)
        stored = runtime.config.profile(body.profile_id) if body.profile_id else None
        if body.profile_id is not None and stored is None:
            raise WebError(404, "model_not_found", f"unknown model {body.profile_id!r}")
        model = body.model or (stored.model if stored else None)
        if not model:
            raise WebError(400, "invalid_request", "model is required")
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
            raise WebError.from_exc(400, "invalid_request", exc) from exc
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
