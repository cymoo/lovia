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
from ...reliability import DEFAULT_MAX_TURNS
from ...providers.anthropic import _DEFAULT_BASE_URL as _ANTHROPIC_BASE_URL
from ...providers.anthropic import _DEFAULT_VERSION as _ANTHROPIC_VERSION
from ...providers.anthropic import _OFFICIAL_HOSTS as _ANTHROPIC_HOSTS
from ...providers.openai_chat import _DEFAULT_BASE_URL as _OPENAI_BASE_URL
from ...providers.openai_chat import _OFFICIAL_HOSTS as _OPENAI_HOSTS
from ..transcribe import OPENAI_PATH, transcription_endpoint


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
SearchProvider = Literal["parallel", "tavily", "exa", "off"]


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


def slugify(text: str, taken: set[str] | None = None) -> str:
    """An id from a display name or model spec, unique among ``taken``:
    starting with a letter or digit, at most 64 characters, whatever the
    text (``model`` when nothing of it survives)."""
    base = _SLUG.sub("-", text.lower()).strip("-._")[:_SLUG_BASE_MAX].rstrip("-._")
    base = base or "model"
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
    # Through the system proxy or not; None: as ``--trust-env`` says.
    trust_env: bool | None = None

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
    """The service ``web_search`` asks: a hosted search MCP server
    (:mod:`lovia.web.search`), or none. ``keys`` holds an optional API key
    per service — each answers without one, within a free allowance."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    provider: SearchProvider = "parallel"
    keys: dict[str, str] = Field(default_factory=dict)
    # Through the system proxy or not; None: as ``--trust-env`` says.
    trust_env: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_backend(cls, data: Any) -> Any:
        # Before 0.9.292 the choice was a backend: Tavily with its key, or the
        # DuckDuckGo scraper. Search turned off stays off; a Tavily key in
        # use keeps Tavily; anything else lands on the default. The key is
        # kept either way, for a way back to Tavily.
        legacy = ("backend", "tavily_api_key")
        if not isinstance(data, dict) or "provider" in data:
            return data
        if not any(name in data for name in legacy):
            return data
        data = dict(data)
        backend = data.pop("backend", "auto")
        key = data.pop("tavily_api_key", None)
        if key:
            data["keys"] = {"tavily": key}
        if backend == "off":
            data["provider"] = "off"
        elif key and backend in ("auto", "tavily"):
            data["provider"] = "tavily"
        return data


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
    # Where the command starts; None: the server's own directory — or, for
    # a project's server, the project.
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    # Honor proxy settings (``trustEnv``); None: lovia's default, off unless
    # ``--trust-env``.
    trust_env: bool | None = None

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
        if command is not None and self.trust_env is not None:
            raise ValueError(f"MCP server {self.name!r}: trustEnv goes with a url")
        if url is not None:
            if self.args or self.env or self.cwd:
                raise ValueError(
                    f"MCP server {self.name!r}: args, env and cwd go with a command"
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
# The turn limit is core's; the retry count is the web's own, for an
# assistant the person works with in a browser (RetryPolicy keeps its own).
DEFAULT_MAX_RETRIES = 5
# The served agent's name — what every chat stores as its agent.
DEFAULT_AGENT_ID = "lovia"
CONFIG_VERSION = 3


class AssistantConfig(BaseModel):
    """How the assistant behaves, for every chat: the ``assistant`` section.

    These are where a new chat starts — a chat picks its own permission
    mode in the composer. ``--readonly`` may narrow ``mode`` — see
    :class:`lovia.web.builder.Deployment`; the file keeps what was chosen.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    # Prepended to the chat's project's AGENTS.md; empty: AGENTS.md alone,
    # or a generic prompt when there is none.
    instructions: str = ""
    mode: WorkspaceModeName = "coding"
    auto_allow: list[AutoAllowBundle] = Field(
        default_factory=lambda: list(DEFAULT_AUTO_ALLOW)
    )
    search: bool = True
    # Long-term memory, kept in <home>/memory.
    memory: bool = True
    subagents: bool = True
    followups: bool = True
    # File-only (Settings does not show them).
    max_turns: int = Field(default=DEFAULT_MAX_TURNS, ge=1)
    # Attempts after the first on transient provider errors.
    max_retries: int = Field(default=DEFAULT_MAX_RETRIES, ge=0)

    @model_validator(mode="after")
    def _normalise(self) -> "AssistantConfig":
        self.__dict__["auto_allow"] = list(dict.fromkeys(self.auto_allow))
        return self


_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")
# An address with its scheme left off: a host, maybe a port, maybe a path.
_BARE_HOST = re.compile(r"^[\w.-]+(:\d+)?(/|$)")


class VoiceConfig(BaseModel):
    """Voice input: where a recorded take goes to become text.

    Not a model profile: a transcription model can't chat, so it must not
    show in the chat's model picker. The endpoint comes from a model
    profile's connection (``connection``, its id) or from ``base_url`` +
    ``api_key`` here; ``base_url`` is an API root or the endpoint's full
    address (:func:`~lovia.web.transcribe.transcription_endpoint`). Off
    until an endpoint is set, and a ``model`` where the endpoint needs one
    — see :meth:`WebConfig.voice_endpoint`.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    connection: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    # ISO 639-1 (``zh``), a regional form where the model takes one
    # (``zh-cn``); None lets the model detect it.
    language: str | None = None

    @model_validator(mode="after")
    def _normalise(self) -> "VoiceConfig":
        clean = {
            name: (getattr(self, name) or "").strip() or None
            for name in ("connection", "base_url", "api_key", "model", "language")
        }
        if clean["base_url"]:
            clean["base_url"] = clean["base_url"].rstrip("/") or None
        if clean["base_url"] and _BARE_HOST.match(clean["base_url"]):
            # Typed the way an address is said: ``api.deepseek.com``.
            clean["base_url"] = "https://" + clean["base_url"]
        if clean["language"]:
            clean["language"] = clean["language"].lower()
            if not _LANGUAGE.match(clean["language"]):
                raise ValueError(
                    f"voice.language {self.language!r} is not a language code "
                    "(like 'zh' or 'en')"
                )
        if clean["connection"] and clean["base_url"]:
            raise ValueError("voice takes a connection or a base_url, not both")
        self.__dict__.update(clean)
        return self


VoiceProblem = Literal["connection_missing", "connection_not_openai", "model_missing"]


class WebConfig(BaseModel):
    """The whole ``config.json`` document.

    Earlier versions kept these settings elsewhere (version 1 in ``agent``,
    ``run`` and ``workspace`` sections, version 2 in an ``agents`` list);
    those are ignored (not converted) and the file is rewritten as version
    3 on the next save.
    """

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    version: int = CONFIG_VERSION
    models: list[ModelProfile] = Field(default_factory=list)
    roles: Roles = Field(default_factory=Roles)
    search: SearchConfig = Field(default_factory=SearchConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    # The enabled ones load in every chat; order is display order.
    mcp: list[MCPServerConfig] = Field(default_factory=list)
    assistant: AssistantConfig = Field(default_factory=AssistantConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    @model_validator(mode="after")
    def _check_references(self) -> "WebConfig":
        self.__dict__["version"] = CONFIG_VERSION
        ids = [p.id for p in self.models]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate model ids: {', '.join(dupes)}")
        known = set(ids)
        # An absent chat role heals to the first profile; a *wrong* reference
        # is a hand-edit typo and must be said out loud.
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

    def voice_problem(self) -> VoiceProblem | None:
        """Why voice input is set up and still off, for Settings to say.

        Deliberately not a load error, unlike a wrong ``roles`` reference:
        deleting or re-pointing a model must not be refused over, or have
        to know about, a setting that only borrows its endpoint.
        """
        voice = self.voice
        if voice.connection is not None:
            profile = self.profile(voice.connection)
            if profile is None:
                return "connection_missing"
            if profile.flavor != "openai":
                return "connection_not_openai"
        target = self._voice_target()
        if target is not None and voice.model is None:
            # The OpenAI route refuses a take that names no model; a
            # service with one route of its own has only the one.
            if urlparse(target[0]).path.endswith(OPENAI_PATH):
                return "model_missing"
        return None

    def _voice_target(self) -> tuple[str, str | None, int | None] | None:
        voice = self.voice
        if voice.connection is not None:
            profile = self.profile(voice.connection)
            if profile is None or profile.flavor != "openai":
                return None
            conn = Connection.from_profile(profile)
            url, limit = transcription_endpoint(
                conn.base_url or OPENAI_FLAVOR.default_base_url, root=True
            )
            return (url, conn.api_key, limit)
        if voice.base_url is not None:
            url, limit = transcription_endpoint(voice.base_url)
            return (url, voice.api_key, limit)
        return None

    def voice_endpoint(self) -> tuple[str, str | None, int | None] | None:
        """``(url, api_key, longest take in seconds)`` voice input
        transcribes at; None: it is off."""
        return None if self.voice_problem() else self._voice_target()


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
    trust_env: bool | None = None

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
            trust_env=profile.trust_env,
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
