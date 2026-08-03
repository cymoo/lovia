"""Configuration engine for the ``lovia web`` CLI.

One engine, several front-ends: the terminal wizard (first run / ``--setup``),
the ``--check`` diagnosis, and the web UI's Settings (upcoming) all read
and write the same ``config.json`` through this package.

- :mod:`.schema` — what the file says (profiles, roles, search).
- :mod:`.storage` — where it lives, and atomic owner-only writes.
- :mod:`.probe` — endpoint validation shared by every front-end.
- :mod:`.wizard` — the interactive terminal flow.
- :mod:`.check` — read-only diagnosis and the startup summary.
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
    Connection,
    ModelProfile,
    ProviderFlavor,
    Roles,
    SearchConfig,
    WebConfig,
    flavor_for_model,
    slugify,
)
from .storage import (
    CONFIG_HINT,
    PROJECT_CONFIG_LABEL,
    USER_CONFIG_LABEL,
    LoadedConfig,
    load_active,
    load_config,
    project_config_path,
    save_config,
    user_config_path,
)
from .wizard import interactive_setup

__all__ = [
    "ANTHROPIC_FLAVOR",
    "CONFIG_HINT",
    "OPENAI_FLAVOR",
    "PROJECT_CONFIG_LABEL",
    "USER_CONFIG_LABEL",
    "ConfigRuntime",
    "Connection",
    "LoadedConfig",
    "ModelProfile",
    "ProviderFlavor",
    "Roles",
    "SearchConfig",
    "ValidationOutcome",
    "WebConfig",
    "build_provider",
    "flavor_for_model",
    "format_app_summary",
    "format_summary",
    "format_unconfigured_summary",
    "interactive_setup",
    "known_context_window",
    "load_active",
    "load_config",
    "mask_key",
    "project_config_path",
    "run_check",
    "save_config",
    "slugify",
    "unlisted_model_note",
    "user_config_path",
    "validate_connection",
]
