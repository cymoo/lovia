"""Configuration schema for the ``lovia web`` CLI.

The model connection, web-search backend and role assignments live in one
JSON document — ``./.lovia/config.json`` (project scope) or
``~/.lovia/config.json`` (user scope) — managed in the web UI's Settings. The file is the single source of truth: there are no
model-connection flags or environment variables. (:mod:`.storage` owns where
the file lives; this module owns what it says.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
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

_SLUG = re.compile(r"[^a-z0-9._-]+")


def slugify(text: str, taken: set[str] | None = None) -> str:
    """A profile id from a display name or model spec, unique among ``taken``."""
    base = _SLUG.sub("-", text.lower()).strip("-.") or "model"
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
    vision: VisionMode = "auto"

    @model_validator(mode="after")
    def _normalise(self) -> "ModelProfile":
        # A vendor-prefixed model decides its own flavor; the field then only
        # matters for bare names (which dialect a custom endpoint speaks).
        if ":" in self.model:
            flavor = flavor_for_model(self.model).name
        else:
            flavor = self.flavor
        base_url = self.base_url.rstrip("/") or None if self.base_url else None
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
    vision: str | None = None  # None -> no see_image delegation
    aux: str | None = None  # titles + follow-ups; None -> the chat model


class SearchConfig(BaseModel):
    """The ``web_search`` tool backend; ``auto`` = Tavily if a key is set."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    backend: SearchBackend = "auto"
    tavily_api_key: str | None = None


class WebConfig(BaseModel):
    """The whole ``config.json`` document."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    version: int = 1
    models: list[ModelProfile] = Field(default_factory=list)
    roles: Roles = Field(default_factory=Roles)
    search: SearchConfig = Field(default_factory=SearchConfig)

    @model_validator(mode="after")
    def _check_references(self) -> "WebConfig":
        ids = [p.id for p in self.models]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate model ids: {', '.join(dupes)}")
        known = set(ids)
        # An absent chat role heals to the first profile; a *wrong* reference
        # (in any role) is a hand-edit typo and must be said out loud.
        if self.roles.chat is None and ids:
            self.roles.__dict__["chat"] = ids[0]
        for role in ("chat", "vision", "aux"):
            ref = getattr(self.roles, role)
            if ref is not None and ref not in known:
                raise ValueError(f"roles.{role} references unknown model id {ref!r}")
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
