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
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...exceptions import UserError
from ...providers import provider_from_string, supports_vision
from ..config.probe import (
    known_context_window,
    unlisted_model_note,
    validate_connection,
)
from ..config.runtime import ConfigRuntime
from ..config.schema import (
    ANTHROPIC_FLAVOR,
    GLOBAL_SKILLS_HINT,
    OPENAI_FLAVOR,
    USER_SKILLS_DIR,
    AutoAllowBundle,
    Connection,
    FlavorName,
    ModelProfile,
    SearchBackend,
    VisionMode,
    WebConfig,
    WorkspaceModeName,
    is_global_dir,
    slugify,
)
from ..auth import is_local_name
from ..config.mcp import parse_servers, restore_secrets, spec_of
from ..config.schema import MCPServerConfig
from ..config.skills import scan_skills_status
from ..errors import WebError
from ..mcp import CONNECT_TIMEOUT, MCPStatus, probe
from ..project_mcp import ProjectServer, read_project_servers
from ..workspaces import WorkspaceRef
from .deps import RouterDeps


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


class AssistantIn(BaseModel):
    """The assistant's settings, as edited; only the fields present change.
    The turn and retry limits are file-only, so naming one here is refused
    rather than quietly dropped."""

    model_config = ConfigDict(extra="forbid")

    instructions: str | None = None
    mode: WorkspaceModeName | None = None
    auto_allow: list[AutoAllowBundle] | None = None
    search: bool | None = None
    memory: bool | None = None
    subagents: bool | None = None
    followups: bool | None = None


# Fixed by --readonly / --no-workspace: the browser can't change them then.
_PERMISSION_FIELDS = frozenset({"mode", "auto_allow"})


class MCPDocumentIn(BaseModel):
    """Servers as pasted into Settings → MCP: an ``mcpServers`` document, or
    the object inside it (:func:`lovia.web.config.mcp.parse_servers`).
    With ``root`` (adding only), they are for that directory's project
    alone: saved switched off, and turned on there."""

    document: Any
    root: str | None = Field(default=None, max_length=4096)


class MCPEnabledIn(BaseModel):
    enabled: bool


class ProjectMCPIn(BaseModel):
    """Turn a server on or off for a project — chat ``session``'s, or the
    directory ``root`` a draft is opening in. ``source`` says which server
    ``name`` is: one the project's ``.mcp.json`` names, or a global one."""

    session: str | None = None
    root: str | None = Field(default=None, max_length=4096)
    name: str
    on: bool
    source: Literal["project", "global"] = "project"


class MCPTestIn(BaseModel):
    """What to test: a saved server by ``name``, or the servers of a
    ``document`` not saved yet — whose masked values are the saved values of
    ``original``, the server being edited."""

    name: str | None = None
    document: Any = None
    original: str | None = None


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


def _sees_images(profile: ModelProfile) -> bool:
    """Whether a chat on ``profile`` takes images inline: its ``vision``
    setting, ``auto`` answered by the provider it builds (official endpoints
    see). An incomplete profile sees none."""
    try:
        conn = Connection.from_profile(profile)
        if conn.model is None:
            return False
        provider = provider_from_string(
            conn.model,
            api_key=conn.api_key,
            base_url=conn.base_url,
            supports_vision=profile.vision_override(),
        )
    except Exception:  # noqa: BLE001 — an incomplete or unknown profile: no
        return False
    return supports_vision(provider)


def _known_window(profile: ModelProfile) -> int | None:
    try:
        return known_context_window(Connection.from_profile(profile))
    except UserError:  # an incomplete profile: nothing to name
        return None


def _status_out(status: MCPStatus | None) -> dict[str, object] | None:
    if status is None:
        return None
    return {
        "ok": status.ok,
        "at": status.at,
        "tools": list(status.tools),
        "error": status.error,
    }


def _parsed(document: Any) -> list[MCPServerConfig]:
    try:
        return parse_servers(document)
    except ValueError as exc:
        raise WebError(400, "invalid_request", str(exc)) from exc


def _with_secrets(
    server: MCPServerConfig, stored: MCPServerConfig | None
) -> MCPServerConfig:
    try:
        return restore_secrets(server, stored)
    except ValueError as exc:
        raise WebError(400, "invalid_request", str(exc)) from exc


def build_config_router(runtime: ConfigRuntime, deps: RouterDeps) -> APIRouter:
    router = APIRouter(prefix="/api/config")

    def _guard_host(request: Request) -> None:
        """Refuse unauthenticated config writes with a non-local Host header.

        Cookie CSRF is already blunted (SameSite=Lax + JSON bodies force a
        preflight); this closes the DNS-rebinding hole a token-less loopback
        deployment would otherwise have. Deployments behind a proxy carry a
        token/auth, which makes the Host header irrelevant.
        """
        if not runtime.require_local_host or is_local_name(request.headers.get("host")):
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
            # None, or why the browser can't change the permissions.
            "lock": deployment.workspace_lock(),
        }

    def _assistant_out() -> dict[str, object]:
        spec = runtime.config.assistant
        return {
            **spec.model_dump(),
            # What runs actually get, once the deployment had its say.
            "effective_mode": runtime.deployment.effective_mode(spec.mode),
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
                    # The window a chat on it has: what is set, else what
                    # the provider can name without asking the endpoint.
                    "window": p.context_window or _known_window(p),
                    "max_tokens": p.max_tokens,
                    "vision": p.vision,
                    # Whether a chat on it sees images: ``vision`` resolved
                    # (``auto`` asks the provider) — the composer's attach.
                    "sees_images": _sees_images(p),
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
            "mcp": _mcp_out(),
            "assistant": _assistant_out(),
            "permissions": _permissions_out(),
        }

    def _mcp_out() -> dict[str, object]:
        used = runtime.project_mcp.turned_on()
        return {
            "timeout": CONNECT_TIMEOUT,
            "servers": [
                {
                    "name": s.name,
                    "enabled": s.enabled,
                    "transport": s.transport,
                    "command": s.command,
                    "args": s.args,
                    "url": s.url,
                    # What the editor shows: env and header values masked.
                    "spec": spec_of(s),
                    "status": _status_out(runtime.mcp.get(s)),
                    # The projects that have it on though it is off here.
                    "projects": [] if s.enabled else used.get(s.name, []),
                }
                for s in runtime.config.mcp
            ],
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
        await _apply(_copy(models=models, roles=roles))
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

    @router.put("/assistant")
    async def update_assistant(
        body: AssistantIn, request: Request
    ) -> dict[str, object]:
        """Apply the fields present, checked against what this deployment
        lets the browser change. ``settings_only``: it names no model, so
        before one is configured it is just saved."""
        _guard_write(request)
        deployment = runtime.deployment
        lock = deployment.workspace_lock()
        stored = runtime.config.assistant.model_dump()
        edited = dict(stored)
        for name in body.model_fields_set:
            value = getattr(body, name)
            if value is None:
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
            edited[name] = value
        await _apply(_copy(assistant=edited), settings_only=True)
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

    # ---- MCP servers ------------------------------------------------------
    # Saving a server is consent to run its command on every message, and a
    # test runs it now: both need a local origin (or the token). Each write
    # reads the list and applies the edited copy under one lock, so two tabs
    # editing at once can't each drop the other's change.
    mcp_lock = asyncio.Lock()

    def _stored_server(name: str) -> MCPServerConfig:
        found = next((s for s in runtime.config.mcp if s.name == name), None)
        if found is None:
            raise WebError(404, "mcp_server_not_found", f"no MCP server {name!r}")
        return found

    async def _apply_servers(servers: list[MCPServerConfig]) -> None:
        # No model is touched: before one is configured this saves for later.
        await _apply(_copy(mcp=[s.model_dump() for s in servers]), settings_only=True)

    @router.post("/mcp", status_code=201)
    async def add_mcp_servers(
        body: MCPDocumentIn, request: Request
    ) -> dict[str, object]:
        _guard_write(request)
        added = [_with_secrets(s, None) for s in _parsed(body.document)]
        project = None
        here: list[str] = []
        if body.root is not None:
            # For one project: off for every other, on there — the same
            # record a switch in that project writes.
            project = await _project(None, body.root)
            if project is None:
                raise WebError(
                    400,
                    "invalid_request",
                    "that directory is no project",
                    hint="add the server for every chat instead",
                )
            # One the document itself has disabled stays off there too.
            here = [s.name for s in added if s.enabled]
            added = [s.model_copy(update={"enabled": False}) for s in added]
        async with mcp_lock:
            taken = {s.name for s in runtime.config.mcp}
            clash = next((s.name for s in added if s.name in taken), None)
            if clash is not None:
                raise WebError(
                    409,
                    "mcp_server_exists",
                    f"an MCP server named {clash!r} exists",
                    hint="edit that one, or give this one another name",
                )
            await _apply_servers([*runtime.config.mcp, *added])
            if project is not None:
                for name in here:
                    runtime.project_mcp.override(project, name, True)
        return _config_out()

    @router.put("/mcp/{name}")
    async def update_mcp_server(
        name: str, body: MCPDocumentIn, request: Request
    ) -> dict[str, object]:
        _guard_write(request)
        parsed = _parsed(body.document)
        if len(parsed) != 1:
            raise WebError(400, "invalid_request", "edit one server at a time")
        async with mcp_lock:
            server = _with_secrets(parsed[0], _stored_server(name))
            if server.name != name and any(
                s.name == server.name for s in runtime.config.mcp
            ):
                raise WebError(
                    409,
                    "mcp_server_exists",
                    f"an MCP server named {server.name!r} exists",
                )
            await _apply_servers(
                [server if s.name == name else s for s in runtime.config.mcp]
            )
            if server.name != name:
                runtime.project_mcp.renamed(name, server.name)
        return _config_out()

    @router.put("/mcp/{name}/enabled")
    async def set_mcp_server_enabled(
        name: str, body: MCPEnabledIn, request: Request
    ) -> dict[str, object]:
        _guard_write(request)
        async with mcp_lock:
            changed = _stored_server(name).model_copy(update={"enabled": body.enabled})
            await _apply_servers(
                [changed if s.name == name else s for s in runtime.config.mcp]
            )
        return _config_out()

    @router.delete("/mcp/{name}")
    async def delete_mcp_server(name: str, request: Request) -> dict[str, object]:
        _guard_write(request)
        async with mcp_lock:
            _stored_server(name)
            await _apply_servers([s for s in runtime.config.mcp if s.name != name])
            runtime.project_mcp.renamed(name, None)
        return _config_out()

    @router.post("/mcp/test")
    async def test_mcp_servers(body: MCPTestIn, request: Request) -> dict[str, object]:
        """Connect, list tools, close — each server at once, each within the
        connect timeout. A saved server's outcome is its last connection.
        Writes nothing, so a read-only ``config.json`` doesn't stop it."""
        _guard_host(request)
        if body.name is not None:
            stored = _stored_server(body.name)
            status = await probe(stored)
            runtime.mcp.record(stored, status)
            results = [(stored.name, status)]
        else:
            original = _stored_server(body.original) if body.original else None
            servers = [_with_secrets(s, original) for s in _parsed(body.document)]
            statuses = await asyncio.gather(*(probe(s) for s in servers))
            results = list(zip((s.name for s in servers), statuses))
        return {"results": [{"name": n, **(_status_out(s) or {})} for n, s in results]}

    # ---- a project's servers: its own (.mcp.json), and the global ones ----

    async def _project(session: str | None, root: str | None) -> str | None:
        """The project of chat ``session`` — or of a draft opening in
        ``root`` — as its runs know it; ``None``: it has none (no
        workspace, or the "Chats" catch-all)."""
        if session is not None:
            meta = await deps.store.get(session)
            if meta is None:
                raise WebError(404, "session_not_found", "session not found")
            binding = deps.chat_binding(deps.pick(deps.agent_of(meta)), meta)
        else:
            binding = deps.new_chat_binding(deps.pick(None), None, root)
        return deps.project_of(binding)

    def _own_servers(key: str | None) -> tuple[list[ProjectServer], str | None]:
        """The project's ``.mcp.json`` — read on this machine only."""
        if key is None or WorkspaceRef.parse(key).remote:
            return [], None
        return read_project_servers(Path(key))

    def _project_out(key: str | None) -> dict[str, object]:
        servers, error = _own_servers(key)
        book = runtime.project_mcp
        states = book.states(key, servers) if key is not None else {}
        here = book.overrides(key) if key is not None else {}
        # A project's server that a run starts stands in for a global one of
        # its name: asked of the rule the run itself uses.
        standing_in = {s.name for s in book.approved(key)} if servers and key else set()
        return {
            "project": key,
            # Whether the project could have a .mcp.json read here at all.
            "local": key is not None and not WorkspaceRef.parse(key).remote,
            "error": error,
            "servers": [
                {
                    "name": s.name,
                    # As written — what turning it on approves.
                    "spec": s.spec,
                    "state": states[s.name],
                    "error": s.error,
                    "status": _status_out(runtime.mcp.get(s.config))
                    if s.config is not None
                    else None,
                }
                for s in servers
            ],
            # Settings → MCP's servers as this project has them.
            "global": [
                {
                    "name": s.name,
                    "spec": spec_of(s),
                    "enabled": s.enabled,
                    "on": here.get(s.name, s.enabled),
                    "replaced": s.name in standing_in,
                    "status": _status_out(runtime.mcp.get(s)),
                }
                for s in runtime.config.mcp
            ],
        }

    @router.get("/project/mcp")
    async def get_project_mcp(
        response: Response,
        session: str | None = Query(None),
        root: str | None = Query(None, max_length=4096),
    ) -> dict[str, object]:
        """The MCP servers of a chat's project: the ones its ``.mcp.json``
        names and whether each is on, and the global ones as this project
        has them. Read from disk every time."""
        response.headers["Cache-Control"] = "no-store"
        return _project_out(await _project(session, root))

    @router.put("/project/mcp")
    async def decide_project_mcp(
        body: ProjectMCPIn, request: Request
    ) -> dict[str, object]:
        """Turn a server on or off for the chat's project: one of its own
        (``source: "project"``), as it is written now, or a global one
        (``"global"``) whatever its switch in Settings says. Turning a
        project's own on is what lets a repository's command run, and the
        record lives in the home beside ``config.json``: guarded like a
        config write. It applies from the chat's next run."""
        _guard_write(request)
        key = await _project(body.session, body.root)
        missing = WebError(
            404, "mcp_server_not_found", f"unknown MCP server {body.name!r}"
        )
        if body.source == "global":
            stored = next((s for s in runtime.config.mcp if s.name == body.name), None)
            if stored is None:
                raise missing
            if key is None:
                raise WebError(
                    400,
                    "invalid_request",
                    "this chat belongs to no project",
                    hint="turn the server on or off for every chat in Settings → MCP",
                )
            # Back on its own switch: nothing to remember.
            choice = None if body.on == stored.enabled else body.on
            runtime.project_mcp.override(key, body.name, choice)
        else:
            server = next(
                (s for s in _own_servers(key)[0] if s.name == body.name), None
            )
            if key is None or server is None:
                raise missing
            runtime.project_mcp.decide(key, server, body.on)
        return _project_out(key)

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
