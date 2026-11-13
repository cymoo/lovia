"""Configuration schema for the ``lovia web`` CLI.

The model connection, web-search backend, role assignments and skill
directories live in one JSON document — ``config.json`` in lovia's home
(``~/.lovia``, or ``lovia web --home DIR``) — managed in the web UI's
Settings. The file is the single source of truth: there are no
model-connection flags or environment variables. (:mod:`.storage` owns where
the file lives; this module owns what it says.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ...providers._http import host_matches
from ...providers.anthropic import _DEFAULT_BASE_URL as _ANTHROPIC_BASE_URL
from ...providers.anthropic import _DEFAULT_VERSION as _ANTHROPIC_VERSION
from ...providers.anthropic import _OFFICIAL_HOSTS as _ANTHROPIC_HOSTS
from ...providers.openai_chat import _DEFAULT_BASE_URL as _OPENAI_BASE_URL
from ...providers.openai_chat import _OFFICIAL_HOSTS as _OPENAI_HOSTS


@dataclass(frozen=True)
class ProviderFlavor:
    """API dialect of the provider a model spec routes to."""

    name: str
    default_base_url: str
    official_hosts: tuple[str, ...]

    def auth_headers(self, api_key: str | None) -> dict[str, str]:
        """Headers for a lightweight authenticated probe of the endpoint."""
        headers: dict[str, str] = {}
        if self.name == "anthropic":
            headers["anthropic-version"] = _ANTHROPIC_VERSION
            if api_key:
                headers["x-api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers


OPENAI_FLAVOR = ProviderFlavor("openai", _OPENAI_BASE_URL, _OPENAI_HOSTS)
ANTHROPIC_FLAVOR = ProviderFlavor("anthropic", _ANTHROPIC_BASE_URL, _ANTHROPIC_HOSTS)

FlavorName = Literal["openai", "anthropic"]
VisionMode = Literal["auto", "on", "off"]
SearchBackend = Literal["auto", "tavily", "ddg", "off"]


def flavor_for_model(spec: str) -> ProviderFlavor:
    """Mirror ``provider_from_string`` routing: anthropic/claude vs the rest."""
    vendor = spec.split(":", 1)[0].lower() if ":" in spec else ""
    if vendor in ("anthropic", "claude"):
        return ANTHROPIC_FLAVOR
    return OPENAI_FLAVOR


# Maps a profile's vision mode onto ``provider_from_string(supports_vision=)``:
# ``auto`` defers to the provider's own capability table.
VISION_OVERRIDES: dict[str, bool | None] = {"auto": None, "on": True, "off": False}

# Request fields the runtime composes from the conversation; a profile's
# ``extra_body`` overriding one would only produce a confusing request.
# Everything else — ``max_tokens`` included — is the user's call.
RESERVED_BODY_FIELDS = frozenset({"model", "messages", "system", "stream", "tools"})

_SLUG = re.compile(r"[^a-z0-9._-]+")
# Ids are at most 64 characters; the base keeps room for a "-NNN" suffix.
_SLUG_BASE_MAX = 56


def slugify(
    text: str, taken: set[str] | None = None, *, fallback: str = "model"
) -> str:
    """An id from a display name or model spec, unique among ``taken``:
    starting with a letter or digit, at most 64 characters, whatever the
    text (``fallback`` when nothing of it survives)."""
    base = _SLUG.sub("-", text.lower()).strip("-._")[:_SLUG_BASE_MAX].rstrip("-._")
    base = base or fallback
    taken = taken or set()
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


class ModelProfile(BaseModel):
    """One configured model endpoint."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=64)
    name: str | None = None
    model: str = Field(min_length=1)
    flavor: FlavorName = "openai"
    base_url: str | None = None
    api_key: str | None = None
    context_window: int | None = Field(default=None, ge=1)
    # Max output tokens per response; None leaves the provider's default.
    max_tokens: int | None = Field(default=None, ge=1)
    vision: VisionMode = "auto"
    # Raw fields merged into every request to this endpoint — the provider's
    # ``extra_body`` (reasoning knobs, vLLM's ``chat_template_kwargs``, ...).
    # Stored as written: the endpoint's own dialect, no translation.
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalise(self) -> "ModelProfile":
        # A vendor-prefixed model decides its own flavor; the field then only
        # matters for bare names (which dialect a custom endpoint speaks).
        if ":" in self.model:
            flavor = flavor_for_model(self.model).name
        else:
            flavor = self.flavor
        base_url = self.base_url.rstrip("/") or None if self.base_url else None
        reserved = sorted(RESERVED_BODY_FIELDS & self.extra_body.keys())
        if reserved:
            raise ValueError(
                f"extra_body cannot set {', '.join(reserved)}: "
                "the runtime composes these from the conversation"
            )
        # Assign via __dict__: plain assignment would re-enter validation.
        self.__dict__["flavor"] = flavor
        self.__dict__["base_url"] = base_url
        return self

    @property
    def display_name(self) -> str:
        return self.name or self.model

    def spec(self) -> str:
        """The ``provider_from_string`` spec this profile routes through."""
        if ":" not in self.model and self.flavor == "anthropic":
            return f"anthropic:{self.model}"
        return self.model

    def vision_override(self) -> bool | None:
        return VISION_OVERRIDES[self.vision]


class Roles(BaseModel):
    """Which profile serves each role; ``None`` falls back sensibly."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    chat: str | None = None  # None -> the first profile
    vision: str | None = None  # None -> no describe_image delegation
    aux: str | None = None  # titles + follow-ups; None -> the chat model


class SearchConfig(BaseModel):
    """The ``web_search`` tool backend; ``auto`` = Tavily if a key is set."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    backend: SearchBackend = "auto"
    tavily_api_key: str | None = None


# The cross-agent conventions (shared with Claude Code, Codex, OpenCode). A
# project's own skills live in its workspace root, beside AGENTS.md, and
# load in every chat there — a convention, not a setting.
PROJECT_SKILLS_DIR = ".agents/skills"
USER_SKILLS_DIR = "~/.agents/skills"
# How the project directory was spelled when it was a configurable entry
# (the default before 0.9.179, so most saved files carry it): it now loads
# anyway, so a saved one is dropped rather than refused.
_LEGACY_PROJECT_ENTRIES = frozenset({PROJECT_SKILLS_DIR, f"./{PROJECT_SKILLS_DIR}"})
GLOBAL_SKILLS_HINT = (
    "skills.dirs takes absolute or ~ paths; a project's own .agents/skills "
    "loads by itself in every chat working there"
)


def is_global_dir(raw: str) -> bool:
    """Whether a skill-directory entry names one directory for every chat."""
    return Path(raw).expanduser().is_absolute()


def global_dirs(dirs: list[str]) -> list[str]:
    """``dirs`` stripped and deduplicated; raises on an entry that is not a
    global path (a relative one would name a different directory in every
    chat). The old project entry is dropped: it loads by itself now."""
    cleaned: list[str] = []
    for raw in dirs:
        value = raw.strip()
        if not value:
            raise ValueError("directory entries must be non-empty paths")
        if value in cleaned or value in _LEGACY_PROJECT_ENTRIES:
            continue
        if not is_global_dir(value):
            raise ValueError(f"{value!r} is not a global path: {GLOBAL_SKILLS_HINT}")
        cleaned.append(value)
    return cleaned


class SkillsConfig(BaseModel):
    """The global skill directories, for every chat the server runs.

    ``dirs`` is precedence-ordered — on a duplicate skill name the first
    root wins, and a project's own ``.agents/skills`` comes before all of
    them. Entries are absolute or ``~`` paths (:func:`global_dirs`).
    Removing the default from the list is how it gets disabled. A listed
    directory that doesn't exist is skipped at build time, never an error:
    a stale path in ``config.json`` must not make the server unbootable.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    dirs: list[str] = Field(default_factory=lambda: [USER_SKILLS_DIR])

    @model_validator(mode="after")
    def _normalise(self) -> "SkillsConfig":
        # __dict__ assignment sidesteps validate_assignment re-entrancy.
        self.__dict__["dirs"] = global_dirs(self.dirs)
        return self


class MCPServerConfig(BaseModel):
    """One MCP server from Settings → MCP: a command to run (stdio) or a URL
    (streamable HTTP), never both. ``name`` prefixes its tools
    (``name__tool``), so it is short and safe in a tool name, and unique.
    ``env`` and ``headers`` hold secrets as often as not: the API masks
    their values (see :mod:`lovia.web.config.mcp`)."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$", max_length=32)
    enabled: bool = True
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_transport(self) -> "MCPServerConfig":
        command = (self.command or "").strip() or None
        url = (self.url or "").strip() or None
        if (command is None) == (url is None):
            raise ValueError(
                f"MCP server {self.name!r} needs either a command or a url"
            )
        if command is not None and self.headers:
            raise ValueError(f"MCP server {self.name!r}: headers go with a url")
        if url is not None:
            if self.args or self.env:
                raise ValueError(
                    f"MCP server {self.name!r}: args and env go with a command"
                )
            if urlparse(url).scheme not in ("http", "https"):
                raise ValueError(f"MCP server {self.name!r}: url must be http(s)")
        self.__dict__["command"] = command
        self.__dict__["url"] = url
        return self

    @property
    def transport(self) -> Literal["stdio", "http"]:
        return "stdio" if self.command else "http"


WorkspaceModeName = Literal["readonly", "coding", "trusted"]
# Named command bundles a ``coding`` workspace runs without asking; the rules
# behind each name live in :mod:`lovia.web.builder`.
AutoAllowBundle = Literal["read-only", "dev-tools"]
DEFAULT_AUTO_ALLOW: tuple[AutoAllowBundle, ...] = ("read-only", "dev-tools")
# The web's own default, for an agent the person works with in a browser;
# core's Runner keeps its library default.
DEFAULT_MAX_TURNS = 100
# The agent a config without any gets — and the one every chat from before
# custom agents was stored under.
DEFAULT_AGENT_ID = "lovia"
CONFIG_VERSION = 2


class AgentConfig(BaseModel):
    """One agent people pick for a chat: who does the work, and how.

    Connections (models, keys, the search backend) are global; an agent
    refers to them and decides whether to use them. ``id`` is what chats
    and schedules store, so it never changes; ``name`` is free to.
    ``--readonly`` may narrow ``mode`` — see
    :class:`lovia.web.builder.Deployment`; the file keeps what was chosen.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    # No dots: it doubles as the core ``Agent.name``, which names tools.
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=64)
    name: str | None = None
    # Prepended to the chat's project's AGENTS.md; empty: AGENTS.md alone,
    # or a generic prompt when there is none.
    instructions: str = ""
    # A model id; None: whichever ``roles.chat`` names.
    model: str | None = None
    mode: WorkspaceModeName = "coding"
    auto_allow: list[AutoAllowBundle] = Field(
        default_factory=lambda: list(DEFAULT_AUTO_ALLOW)
    )
    # Skill directories; None: the global ones. A project's own load anyway.
    skills: list[str] | None = None
    search: bool = True
    # The enabled servers of Settings → MCP; off: none of them.
    mcp: bool = True
    # Long-term memory, kept in <home>/memory and shared by every agent.
    memory: bool = True
    subagents: bool = True
    max_turns: int = Field(default=DEFAULT_MAX_TURNS, ge=1)
    # Attempts after the first on transient provider errors; None keeps the
    # core default posture.
    max_retries: int | None = Field(default=None, ge=0)
    followups: bool = True

    @model_validator(mode="after")
    def _normalise(self) -> "AgentConfig":
        name = (self.name or "").strip()
        self.__dict__["name"] = name or None
        self.__dict__["auto_allow"] = list(dict.fromkeys(self.auto_allow))
        if self.skills is not None:
            self.__dict__["skills"] = global_dirs(self.skills)
        return self

    @property
    def display_name(self) -> str:
        return self.name or self.id


def agent_slug(name: str, taken: set[str]) -> str:
    """A new agent's id from its name (``agent`` when nothing survives)."""
    return slugify(name.replace(".", "-"), taken, fallback="agent")


class WebConfig(BaseModel):
    """The whole ``config.json`` document.

    Version 1 kept one agent's settings in ``agent``, ``run`` and
    ``workspace`` sections; those are ignored (not converted) and the file
    is rewritten as version 2 on the next save.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    version: int = CONFIG_VERSION
    models: list[ModelProfile] = Field(default_factory=list)
    roles: Roles = Field(default_factory=Roles)
    search: SearchConfig = Field(default_factory=SearchConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    # For every agent whose ``mcp`` switch is on; order is display order.
    mcp: list[MCPServerConfig] = Field(default_factory=list)
    # The first is the default: what a new chat starts with, and what a chat
    # whose agent was deleted runs on.
    agents: list[AgentConfig] = Field(
        default_factory=lambda: [AgentConfig(id=DEFAULT_AGENT_ID)]
    )

    @model_validator(mode="after")
    def _check_references(self) -> "WebConfig":
        self.__dict__["version"] = CONFIG_VERSION
        ids = [p.id for p in self.models]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate model ids: {', '.join(dupes)}")
        known = set(ids)
        # An absent chat role heals to the first profile; a *wrong* reference
        # (in any role, or an agent's model) is a hand-edit typo and must be
        # said out loud.
        if self.roles.chat is None and ids:
            self.roles.__dict__["chat"] = ids[0]
        for role in ("chat", "vision", "aux"):
            ref = getattr(self.roles, role)
            if ref is not None and ref not in known:
                raise ValueError(f"roles.{role} references unknown model id {ref!r}")
        names = [s.name for s in self.mcp]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate MCP server names: {', '.join(dupes)}")
        if not self.agents:
            self.__dict__["agents"] = [AgentConfig(id=DEFAULT_AGENT_ID)]
        agent_ids = [a.id for a in self.agents]
        dupes = sorted({i for i in agent_ids if agent_ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate agent ids: {', '.join(dupes)}")
        for agent in self.agents:
            if agent.model is not None and agent.model not in known:
                raise ValueError(
                    f"agent {agent.id!r} references unknown model id {agent.model!r}"
                )
        return self

    def profile(self, profile_id: str | None) -> ModelProfile | None:
        if profile_id is None:
            return None
        return next((p for p in self.models if p.id == profile_id), None)

    def default_profile(self) -> ModelProfile | None:
        return self.profile(self.roles.chat)

    def vision_profile(self) -> ModelProfile | None:
        return self.profile(self.roles.vision)

    def aux_profile(self) -> ModelProfile | None:
        return self.profile(self.roles.aux)

    @property
    def default_agent(self) -> AgentConfig:
        return self.agents[0]

    def agent(self, agent_id: str) -> AgentConfig | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def agent_profile(self, agent: AgentConfig) -> ModelProfile | None:
        """The model ``agent`` runs on: its own, else the default chat one."""
        return self.profile(agent.model or self.roles.chat)

    def agent_skills(self, agent: AgentConfig) -> SkillsConfig:
        """The global skill directories ``agent`` loads."""
        if agent.skills is None:
            return self.skills
        return SkillsConfig(dirs=agent.skills)


@dataclass
class Connection:
    """One connection as launched or probed: a profile with defaults filled.

    ``context_window`` may be adopted from a ``/models`` probe;
    ``window_from_endpoint`` marks that case so the value is used for this
    launch but never persisted (it would go on lying after the deployment is
    resized).
    """

    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    context_window: int | None = None
    window_from_endpoint: bool = False
    # Runtime probe data, never persisted: the model ids a successful
    # ``/models`` probe listed (None until then, or when the shape is foreign).
    available_models: list[str] | None = None

    @property
    def flavor(self) -> ProviderFlavor | None:
        return flavor_for_model(self.model) if self.model else None

    @classmethod
    def from_profile(cls, profile: ModelProfile) -> "Connection":
        """A launch-ready connection: the profile with defaults filled in."""
        spec = profile.spec()
        base_url = profile.base_url or flavor_for_model(spec).default_base_url
        return cls(
            model=spec,
            base_url=base_url.rstrip("/"),
            api_key=profile.api_key,
            context_window=profile.context_window,
        )

    def needs_api_key(self) -> bool:
        """True when the endpoint is an official host that requires a key.

        Same rule as the providers' ``_check_ready``: keyless custom gateways
        are fine, the official APIs are not.
        """
        if self.api_key or not self.base_url:
            return False
        flavor = self.flavor
        if flavor is None:
            return False
        host = urlparse(self.base_url).hostname
        return host_matches(host, flavor.official_hosts)

    def missing(self) -> list[str]:
        """Required values that the configuration did not supply."""
        if self.model is None:
            return ["model"]
        return ["API key"] if self.needs_api_key() else []
