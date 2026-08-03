"""The interactive terminal wizard behind first runs and ``--setup``.

Asks for whatever the configuration is missing (everything, on a first run),
validates the freshly entered connection against the endpoint, and offers to
persist it to ``config.json``. Session semantics on a declined save: the
entered connection still serves this launch, it just isn't written down.
"""

from __future__ import annotations

import getpass as _getpass
import sys
from typing import Callable, TextIO

import httpx

from ...exceptions import UserError
from .check import mask_key
from .probe import (
    ValidationOutcome,
    known_context_window,
    unlisted_model_note,
    validate_connection,
)
from .schema import Connection, ModelProfile, slugify
from .storage import (
    CONFIG_HINT,
    PROJECT_CONFIG_LABEL,
    USER_CONFIG_LABEL,
    LoadedConfig,
    project_config_path,
    save_config,
    user_config_path,
)


def interactive_setup(
    loaded: LoadedConfig,
    *,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = _getpass.getpass,
    transport: httpx.BaseTransport | None = None,
    out: TextIO = sys.stdout,
    reconfigure: bool = False,
) -> Connection:
    """Run the wizard over ``loaded`` and return the connection to launch with.

    Only items the configuration is missing are asked — unless ``reconfigure``
    (the ``--setup`` flag), which revisits every value with the current one as
    the Enter-keeps default. The entered profile is upserted into
    ``loaded.config`` in memory either way; persisting it is the user's call.
    Raises :class:`UserError` when stdin closes mid-prompt;
    ``KeyboardInterrupt`` propagates so the CLI's handler can exit with 130.
    """
    try:
        return _run_wizard(
            loaded,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
            transport=transport,
            out=out,
            reconfigure=reconfigure,
        )
    except EOFError as exc:
        raise UserError(
            "interactive setup aborted (stdin closed)", hint=CONFIG_HINT
        ) from exc


def _run_wizard(
    loaded: LoadedConfig,
    *,
    input_fn: Callable[[str], str],
    getpass_fn: Callable[[str], str],
    transport: httpx.BaseTransport | None,
    out: TextIO,
    reconfigure: bool,
) -> Connection:
    def say(message: str) -> None:
        print(message, file=out)

    say("")
    if reconfigure:
        say("reconfigure the model endpoint — Enter keeps the current value.")
    else:
        say("lovia needs a model endpoint — a few seconds now, saved for next time.")
    say("")

    profile = loaded.config.default_profile()
    conn = Connection.from_profile(profile) if profile else Connection()

    ask_base = True
    if conn.model is None:
        say("  examples: openai:gpt-5.5 · anthropic:claude-sonnet-4-5")
        say("            deepseek-v4-pro (bare name = any OpenAI-compatible endpoint)")
        while not conn.model:
            conn.model = input_fn("  Model: ").strip() or None
        flavor = conn.flavor
        assert flavor is not None
        conn.base_url = flavor.default_base_url.rstrip("/")
    elif reconfigure:
        answer = input_fn(f"  Model [{conn.model}]: ").strip()
        if answer and answer != conn.model:
            old_flavor = conn.flavor
            conn.model = answer
            # The window is a property of the model: a stale one must not
            # survive a model change (the probe/table re-supply it below).
            conn.context_window, conn.window_from_endpoint = None, False
            if conn.flavor is not old_flavor:
                # New vendor prefix: the old flavor's endpoint and key are
                # meaningless — fall back to the new flavor's default.
                assert conn.flavor is not None
                conn.base_url = conn.flavor.default_base_url.rstrip("/")
                conn.api_key = None
    else:
        # A configured model with something missing (e.g. a hand-written
        # config without the key): ask only for what's absent.
        ask_base = False

    if ask_base:
        answer = input_fn(f"  Base URL [{conn.base_url}]: ").strip()
        if answer:
            conn.base_url = answer.rstrip("/")

    if conn.api_key is None:
        _prompt_api_key(conn, getpass_fn=getpass_fn, out=out)
    elif reconfigure:
        key = getpass_fn(f"  API key [{mask_key(conn.api_key)}, Enter keeps]: ").strip()
        if key:
            conn.api_key = key

    _validation_loop(
        conn, input_fn=input_fn, getpass_fn=getpass_fn, transport=transport, out=out
    )
    _maybe_prompt_context_window(conn, input_fn=input_fn, out=out)
    _persist(loaded, conn, input_fn=input_fn, out=out, reconfigure=reconfigure)
    if not reconfigure:
        say("  change anytime: lovia web --setup")
    return conn


def _prompt_api_key(
    conn: Connection,
    *,
    getpass_fn: Callable[[str], str],
    out: TextIO,
    required: bool | None = None,
) -> None:
    required = conn.needs_api_key() if required is None else required
    if required:
        prompt = "  API key (hidden, required here): "
    else:
        prompt = "  API key (hidden, Enter to skip): "
    while True:
        key = getpass_fn(prompt).strip()
        if key:
            conn.api_key = key
            return
        if not required:
            return
        print("  an API key is required for this endpoint", file=out)


def _validation_loop(
    conn: Connection,
    *,
    input_fn: Callable[[str], str],
    getpass_fn: Callable[[str], str],
    transport: httpx.BaseTransport | None,
    out: TextIO,
) -> None:
    def say(message: str) -> None:
        print(message, file=out)

    while True:
        outcome, detail = validate_connection(conn, transport=transport)
        if outcome is ValidationOutcome.OK:
            say(f"  ✓ endpoint reachable ({conn.base_url})")
            note = unlisted_model_note(conn)
            if note:
                say(f"  {note}")
            return
        if outcome is ValidationOutcome.UNVERIFIABLE:
            say(f"  note: could not verify the endpoint ({detail}); continuing")
            return
        if outcome is ValidationOutcome.AUTH_FAILED:
            say(f"  ✗ authentication failed ({detail}); enter the key again")
            _prompt_api_key(conn, getpass_fn=getpass_fn, out=out, required=True)
        else:  # UNREACHABLE
            say(f"  ✗ cannot reach {conn.base_url} ({detail})")
            answer = input_fn(f"  Base URL [Enter to retry {conn.base_url}]: ").strip()
            if answer:
                conn.base_url = answer.rstrip("/")


def _maybe_prompt_context_window(
    conn: Connection, *, input_fn: Callable[[str], str], out: TextIO
) -> None:
    """Ask for the compaction window only when the provider can't report it."""
    if conn.context_window is not None or conn.model is None:
        return
    if known_context_window(conn) is not None:
        return
    print(
        "  this model's context window is unknown — set it for proactive"
        " compaction, or let long chats fall back to overflow handling",
        file=out,
    )
    while True:
        raw = input_fn("  Context window in tokens [Enter = automatic]: ").strip()
        if not raw:
            return
        try:
            value = int(raw)
        except ValueError:
            print(f"  invalid integer for context window: {raw!r}", file=out)
            continue
        if value < 1:
            print(f"  context window must be >= 1, got {value}", file=out)
            continue
        conn.context_window = value
        return


def _persist(
    loaded: LoadedConfig,
    conn: Connection,
    *,
    input_fn: Callable[[str], str],
    out: TextIO,
    reconfigure: bool,
) -> None:
    """Upsert the entered connection into the config; offer to write it down."""
    assert conn.model is not None and conn.flavor is not None
    config = loaded.config
    existing = config.default_profile()
    profile_id = (
        existing.id if existing else slugify(conn.model, {p.id for p in config.models})
    )
    # The flavor's default base URL is not pinned, and an endpoint-reported
    # window is never persisted (it would go on lying after the deployment
    # is resized).
    default_base = conn.flavor.default_base_url.rstrip("/")
    profile = ModelProfile(
        id=profile_id,
        model=conn.model,
        base_url=conn.base_url if conn.base_url != default_base else None,
        api_key=conn.api_key,
        context_window=None if conn.window_from_endpoint else conn.context_window,
    )
    models = [p for p in config.models if p.id != profile_id]
    config.models = [*models, profile]
    config.roles.chat = profile_id

    if reconfigure:
        # Default to the scope the configuration currently lives in: saving
        # user-level while a project file wins here would look like a no-op.
        default = "p" if loaded.label == PROJECT_CONFIG_LABEL else "u"
        answer = (
            input_fn(
                f"  Save to [u] {USER_CONFIG_LABEL} (any directory) · "
                f"[p] ./{PROJECT_CONFIG_LABEL} (this directory) · "
                f"[n] don't save [{default}]: "
            )
            .strip()
            .lower()
            or default
        )
        if answer.startswith("n"):
            loaded.exists = False  # the session's config differs from any file
            print(
                "  not saved; this configuration applies to this launch only",
                file=out,
            )
            return
        user_scope = not answer.startswith("p")
    else:
        answer = input_fn(
            f"  Save to {USER_CONFIG_LABEL} (used from any directory)? [Y/n]: "
        )
        if answer.strip().lower() not in ("", "y", "yes"):
            loaded.exists = False
            print(
                "  not saved; this configuration applies to this launch only",
                file=out,
            )
            return
        user_scope = True

    path = user_config_path() if user_scope else project_config_path()
    save_config(config, path)
    loaded.path = path
    loaded.label = USER_CONFIG_LABEL if user_scope else PROJECT_CONFIG_LABEL
    loaded.exists = True
    label = USER_CONFIG_LABEL if user_scope else f"./{PROJECT_CONFIG_LABEL}"
    print(f"  saved to {label} — owner-only, git-ignored", file=out)
    if user_scope and project_config_path().is_file():
        print(
            f"  note: ./{PROJECT_CONFIG_LABEL} exists and wins in this "
            "directory; the user-scope save applies elsewhere",
            file=out,
        )
