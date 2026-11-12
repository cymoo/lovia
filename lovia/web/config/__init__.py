"""Configuration engine for the ``lovia web`` CLI.

The web UI's Settings is the one write path (over ``/api/config``); the
``--check`` diagnosis and the CLI boot read the same ``config.json``
through this package. Hand-editing the file stays possible — it is
validated wholesale on load.

- :mod:`.schema` — what the file says (profiles, roles, search, skills).
- :mod:`.storage` — where it lives (lovia's home), and atomic owner-only writes.
- :mod:`.probe` — endpoint validation shared by every front-end.
- :mod:`.runtime` — building and hot-swapping the served agent.
- :mod:`.check` — read-only diagnosis and the startup summary.
- :mod:`.skills` — skill-directory resolution and discovery.
"""

from __future__ import annotations

from .check import (
    format_app_summary,
    format_summary,
    format_unconfigured_summary,
    mask_key,
    run_check,
)
from .probe import (
    ValidationOutcome,
    build_provider,
    known_context_window,
    unlisted_model_note,
    validate_connection,
)
from .runtime import ConfigRuntime
from .schema import (
    ANTHROPIC_FLAVOR,
    OPENAI_FLAVOR,
    PROJECT_SKILLS_DIR,
    USER_SKILLS_DIR,
    AgentSettings,
    Connection,
    ModelProfile,
    ProviderFlavor,
    Roles,
    RunSettings,
    SearchConfig,
    SkillsConfig,
    WebConfig,
    WorkspaceSettings,
    flavor_for_model,
    slugify,
)
from .skills import (
    RootStatus,
    resolve_skills_dirs,
    scan_skills_status,
)
from .storage import (
    CONFIG_HINT,
    DEFAULT_HOME,
    LoadedConfig,
    config_path,
    config_writable,
    display_path,
    load_active,
    load_config,
    protect_home,
    save_config,
)

__all__ = [
    "ANTHROPIC_FLAVOR",
    "CONFIG_HINT",
    "DEFAULT_HOME",
    "OPENAI_FLAVOR",
    "PROJECT_SKILLS_DIR",
    "USER_SKILLS_DIR",
    "AgentSettings",
    "ConfigRuntime",
    "Connection",
    "LoadedConfig",
    "ModelProfile",
    "ProviderFlavor",
    "Roles",
    "RootStatus",
    "RunSettings",
    "SearchConfig",
    "SkillsConfig",
    "ValidationOutcome",
    "WebConfig",
    "WorkspaceSettings",
    "build_provider",
    "config_path",
    "config_writable",
    "display_path",
    "flavor_for_model",
    "format_app_summary",
    "format_summary",
    "format_unconfigured_summary",
    "known_context_window",
    "load_active",
    "load_config",
    "mask_key",
    "protect_home",
    "resolve_skills_dirs",
    "run_check",
    "save_config",
    "scan_skills_status",
    "slugify",
    "unlisted_model_note",
    "validate_connection",
]
