"""First-run onboarding for the lovia web CLI.

Resolves the model connection (model id, base URL, API key, context window)
from flags and environment layers with per-value source tracking, prompts
interactively for whatever is missing, validates freshly entered values
against the endpoint, and offers to persist them (owner-only, git-ignored) —
to ``~/.lovia/config.env`` by default so one setup works from any directory,
or to the project-local ``./.lovia/config.env``. Both auto-load on later
launches at the lowest precedence:
flag > environment > ``./.lovia/config.env`` > ``~/.lovia/config.env``.
``interactive_setup(reconfigure=True)`` (the ``--setup`` flag) re-runs the
wizard over a complete configuration, and :func:`run_check` (``--check``) is
the read-only diagnosis of the same resolution.

Everything here is CLI-only: the embeddable core (``create_app`` / ``serve``)
never loads env files, so an app embedding lovia brings its own config.
"""

from __future__ import annotations

import difflib
import enum
import getpass as _getpass
import logging
import os
import re
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, TextIO
from urllib.parse import urlparse

import httpx

from ..exceptions import UserError
from ..http_config import resolve_trust_env, resolve_verify
from ..providers import Provider, model_from_env, provider_from_string
from ..providers._http import host_matches
from ..providers.anthropic import _DEFAULT_BASE_URL as _ANTHROPIC_BASE_URL
from ..providers.anthropic import _DEFAULT_VERSION as _ANTHROPIC_VERSION
from ..providers.anthropic import _OFFICIAL_HOSTS as _ANTHROPIC_HOSTS
from ..providers._windows import window_from_models_payload
from ..providers.base import context_window as provider_context_window
from ..providers.openai_chat import _DEFAULT_BASE_URL as _OPENAI_BASE_URL
from ..providers.openai_chat import _OFFICIAL_HOSTS as _OPENAI_HOSTS

log = logging.getLogger("lovia.web.setup")

# The non-interactive ways to configure a value — shown when there's no TTY.
# Names both key vars since the required one follows the model's vendor prefix.
CONFIG_HINT = (
    "pass --model and --api-key, or set "
    "LOVIA_MODEL and OPENAI_API_KEY / ANTHROPIC_API_KEY"
)

# Saved config lives beside the chat DB under the CWD-relative .lovia/ dir — a
# lovia-owned home, so it never collides with a generic ./.env another tool
# might read, and (unlike a bare ~/.env) it stays tidy when launched from $HOME.
CONFIG_DIR = Path(".lovia")


def config_path() -> Path:
    """Project-scope config (``./.lovia/config.env``) — this directory only."""
    return CONFIG_DIR / "config.env"


def user_config_path() -> Path:
    """User-scope config (``~/.lovia/config.env``) — used from any directory."""
    return Path("~/.lovia/config.env").expanduser()


# Display labels for the two auto-loaded scopes: the provenance names shown in
# the startup summary, and how a reconfigure save picks its default target.
PROJECT_CONFIG_LABEL = ".lovia/config.env"
USER_CONFIG_LABEL = "~/.lovia/config.env"


# Where a resolved value came from; shown in the startup summary and used to
# decide what the interactive wizard still needs to ask and what to persist.
Source = str
_UNSET = "unset"


@dataclass(frozen=True)
class ProviderFlavor:
    """API dialect of the provider a model spec routes to."""

    name: str
    env_prefix: str
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


OPENAI_FLAVOR = ProviderFlavor("openai", "OPENAI", _OPENAI_BASE_URL, _OPENAI_HOSTS)
ANTHROPIC_FLAVOR = ProviderFlavor(
    "anthropic", "ANTHROPIC", _ANTHROPIC_BASE_URL, _ANTHROPIC_HOSTS
)


def flavor_for_model(spec: str) -> ProviderFlavor:
    """Mirror ``provider_from_string`` routing: anthropic/claude vs the rest."""
    vendor = spec.split(":", 1)[0].lower() if ":" in spec else ""
    if vendor in ("anthropic", "claude"):
        return ANTHROPIC_FLAVOR
    return OPENAI_FLAVOR


@dataclass
class Connection:
    """The model endpoint configuration with per-value provenance."""

    model: str | None = None
    model_source: Source = _UNSET
    base_url: str | None = None
    base_url_source: Source = _UNSET
    api_key: str | None = None
    api_key_source: Source = _UNSET
    context_window: int | None = None
    context_window_source: Source = _UNSET
    # Runtime probe data, never persisted: the model ids a successful
    # ``/models`` probe listed (None until then, or when the shape is foreign).
    available_models: list[str] | None = None

    @property
    def flavor(self) -> ProviderFlavor | None:
        return flavor_for_model(self.model) if self.model else None

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
        """Required values that no configuration channel supplied."""
        if self.model is None:
            return ["model"]
        return ["API key"] if self.needs_api_key() else []


def _env_value(name: str, env_sources: Mapping[str, str]) -> tuple[str | None, Source]:
    value = os.getenv(name)
    if not value:
        return None, _UNSET
    return value, env_sources.get(name, "env")


def _parse_context_window(raw: str, *, what: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise UserError(f"invalid integer for {what}: {raw!r}") from exc
    if value < 1:
        raise UserError(f"{what} must be >= 1, got {value}")
    return value


def _derive_endpoint(conn: Connection, env_sources: Mapping[str, str]) -> None:
    """Fill env/default values for fields no earlier layer has claimed."""
    flavor = conn.flavor
    if flavor is None:
        return
    if conn.base_url_source == _UNSET or conn.base_url_source == "default":
        value, source = _env_value(f"{flavor.env_prefix}_BASE_URL", env_sources)
        if value:
            conn.base_url, conn.base_url_source = value.rstrip("/"), source
        else:
            conn.base_url = flavor.default_base_url.rstrip("/")
            conn.base_url_source = "default"
    if conn.api_key is None:
        value, source = _env_value(f"{flavor.env_prefix}_API_KEY", env_sources)
        if value:
            conn.api_key, conn.api_key_source = value, source


def resolve_connection(
    *,
    model_flag: str | None,
    base_url_flag: str | None,
    api_key_flag: str | None,
    context_window_flag: int | None,
    env_sources: Mapping[str, str],
) -> Connection:
    """Resolve the connection from flags and the (already loaded) env layers."""
    conn = Connection()
    if model_flag:
        conn.model, conn.model_source = model_flag, "flag"
    else:
        model = model_from_env(required=False)
        if model:
            conn.model = model
            conn.model_source = env_sources.get("LOVIA_MODEL", "env")
    if base_url_flag:
        conn.base_url, conn.base_url_source = base_url_flag.rstrip("/"), "flag"
    if api_key_flag:
        conn.api_key, conn.api_key_source = api_key_flag, "flag"
    if context_window_flag is not None:
        if context_window_flag < 1:
            raise UserError(f"--context-window must be >= 1, got {context_window_flag}")
        conn.context_window, conn.context_window_source = context_window_flag, "flag"
    else:
        raw, source = _env_value("LOVIA_CONTEXT_WINDOW", env_sources)
        if raw:
            conn.context_window = _parse_context_window(
                raw, what="LOVIA_CONTEXT_WINDOW"
            )
            conn.context_window_source = source
    _derive_endpoint(conn, env_sources)
    return conn


# ------------------------------------------------------------- validation -


class ValidationOutcome(enum.Enum):
    OK = "ok"
    AUTH_FAILED = "auth_failed"
    UNREACHABLE = "unreachable"
    UNVERIFIABLE = "unverifiable"


def validate_connection(
    conn: Connection,
    *,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[ValidationOutcome, str]:
    """Probe ``GET {base_url}/models`` and classify the response.

    Only called for interactively entered values — configured launches never
    pay for this request. A successful body doubles as a context-window
    source: vLLM, SGLang, OpenRouter, Groq and Together publish the model's
    window there, so the wizard need not ask for a number the endpoint knows.
    """
    assert conn.base_url is not None and conn.flavor is not None
    try:
        with httpx.Client(
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
            trust_env=resolve_trust_env(None),
            verify=resolve_verify(),
        ) as client:
            response = client.get(
                f"{conn.base_url}/models",
                headers=conn.flavor.auth_headers(conn.api_key),
            )
    except httpx.TransportError as exc:
        return ValidationOutcome.UNREACHABLE, str(exc) or type(exc).__name__
    if response.status_code in (401, 403):
        return ValidationOutcome.AUTH_FAILED, f"HTTP {response.status_code}"
    if response.is_success:
        _adopt_reported_window(conn, response)
        conn.available_models = _listed_model_ids(response)
        return ValidationOutcome.OK, f"HTTP {response.status_code}"
    return ValidationOutcome.UNVERIFIABLE, f"HTTP {response.status_code}"


def _listed_model_ids(response: httpx.Response) -> list[str] | None:
    """Model ids from a ``/models`` body; None when the shape is foreign."""
    try:
        payload = response.json()
    except ValueError:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    ids = [
        entry["id"]
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]
    return ids or None


def _adopt_reported_window(conn: Connection, response: httpx.Response) -> None:
    """Take the window from a ``/models`` body, unless the user set one."""
    if conn.context_window is not None or conn.model is None:
        return
    try:
        payload = response.json()
    except ValueError:
        return  # not every /models endpoint answers with JSON
    window = window_from_models_payload(payload, conn.model)
    if window is not None:
        conn.context_window, conn.context_window_source = window, "endpoint"


# ------------------------------------------------------------ persistence -


def _protect_config_dir(directory: Path) -> None:
    """Create lovia's data dir, make it owner-only, and drop a ``*`` .gitignore.

    The dir holds saved secrets and the chat DB, so it's ``0700`` where the OS
    honours it (Windows ignores mode bits) and its whole contents are git-ignored
    — never committed inside someone's repo."""
    directory.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):  # best-effort; Windows ignores mode bits
        directory.chmod(0o700)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# lovia data — secrets and chat history; never commit.\n*\n",
            encoding="utf-8",
        )


_CONFIG_HEADER = (
    "# lovia config — KEY=value, loaded at launch; the process environment\n"
    "# overrides values here. Managed by `lovia web --setup`; hand edits and\n"
    "# comments are preserved.\n"
)

_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def save_env_file(values: Mapping[str, str], path: Path | None = None) -> Path:
    """Upsert plain ``KEY=value`` lines into a config file (created if missing).

    Hand edits survive: unknown keys, comments and ordering are preserved
    byte-for-byte, a saved key replaces its first occurrence in place, and
    later duplicates of that key (the pre-0.9.10 append-only format) are
    dropped. New keys are appended; a fresh file starts with a short header.
    The file holds API keys, so it's written owner-only (``0600``); a
    ``.lovia/`` target dir (either scope) is made ``0700`` and git-ignored.
    """
    path = path or config_path()
    if path.parent.name == CONFIG_DIR.name:
        _protect_config_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        content = _upsert(path.read_text(encoding="utf-8"), values)
    else:
        content = _CONFIG_HEADER + "".join(f"{k}={v}\n" for k, v in values.items())
    path.write_text(content, encoding="utf-8")
    with suppress(OSError):  # best-effort; Windows ignores mode bits
        path.chmod(0o600)
    return path


def _upsert(existing: str, values: Mapping[str, str]) -> str:
    """Replace each key's first occurrence in place; keep everything else."""
    pending = dict(values)
    replaced: set[str] = set()
    out: list[str] = []
    for line in existing.splitlines():
        match = _ENV_LINE.match(line)
        key = match.group(1) if match else None
        if key is not None and key in replaced:
            continue  # append-only-era duplicate of a key just rewritten
        if key is not None and key in pending:
            out.append(f"{key}={pending.pop(key)}")
            replaced.add(key)
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in pending.items())
    return "\n".join(out) + "\n"


# ------------------------------------------------------------ interaction -


def interactive_setup(
    conn: Connection,
    *,
    env_sources: Mapping[str, str],
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = _getpass.getpass,
    transport: httpx.BaseTransport | None = None,
    out: TextIO = sys.stdout,
    reconfigure: bool = False,
) -> Connection:
    """Ask for the missing connection values, validate, offer to persist.

    Only items no configuration channel supplied are asked — unless
    ``reconfigure`` (the ``--setup`` flag), which revisits every value with
    the current one as the Enter-keeps default. Raises :class:`UserError`
    when stdin closes mid-prompt; ``KeyboardInterrupt`` propagates so the
    CLI's existing handler can exit with 130.
    """
    try:
        return _run_wizard(
            conn,
            env_sources=env_sources,
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
    conn: Connection,
    *,
    env_sources: Mapping[str, str],
    input_fn: Callable[[str], str],
    getpass_fn: Callable[[str], str],
    transport: httpx.BaseTransport | None,
    out: TextIO,
    reconfigure: bool = False,
) -> Connection:
    def say(message: str) -> None:
        print(message, file=out)

    say("")
    if reconfigure:
        say("reconfigure the model endpoint — Enter keeps the current value.")
    else:
        say("lovia needs a model endpoint — a few seconds now, saved for next time.")
    say("")

    if conn.model is None:
        say("  examples: openai:gpt-5.5 · anthropic:claude-sonnet-4-5")
        say("            deepseek-v4-pro (bare name = any OpenAI-compatible endpoint)")
        while not conn.model:
            conn.model = input_fn("  Model: ").strip() or None
        conn.model_source = "prompt"
    elif reconfigure:
        answer = input_fn(f"  Model [{conn.model}]: ").strip()
        if answer and answer != conn.model:
            old_flavor = conn.flavor
            conn.model, conn.model_source = answer, "prompt"
            # The window is a property of the model: a stale one must not
            # survive a model change (the probe/table re-supply it below).
            conn.context_window, conn.context_window_source = None, _UNSET
            if conn.flavor is not old_flavor:
                # New vendor prefix: the old flavor's endpoint and key are
                # meaningless — re-derive from the new flavor's env/defaults.
                conn.base_url, conn.base_url_source = None, _UNSET
                conn.api_key, conn.api_key_source = None, _UNSET
    # The flavor may be known only now: pull in its env defaults before
    # deciding what else to ask.
    _derive_endpoint(conn, env_sources)

    flavor = conn.flavor
    assert flavor is not None

    if conn.base_url_source == "default" or reconfigure:
        answer = input_fn(f"  Base URL [{conn.base_url}]: ").strip()
        if answer:
            conn.base_url = answer.rstrip("/")
        conn.base_url_source = "prompt"

    if conn.api_key is None:
        _prompt_api_key(conn, getpass_fn=getpass_fn, out=out)
    elif reconfigure:
        key = getpass_fn(f"  API key [{mask_key(conn.api_key)}, Enter keeps]: ").strip()
        if key:
            conn.api_key, conn.api_key_source = key, "prompt"

    _validation_loop(
        conn, input_fn=input_fn, getpass_fn=getpass_fn, transport=transport, out=out
    )
    _maybe_prompt_context_window(conn, input_fn=input_fn, out=out)
    _offer_to_save(
        conn,
        input_fn=input_fn,
        out=out,
        env_sources=env_sources,
        reconfigure=reconfigure,
    )
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
            conn.api_key, conn.api_key_source = key, "prompt"
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
            _warn_unlisted_model(conn, out)
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
                conn.base_url, conn.base_url_source = answer.rstrip("/"), "prompt"


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
            conn.context_window = _parse_context_window(raw, what="context window")
        except UserError as exc:
            print(f"  {exc}", file=out)
            continue
        conn.context_window_source = "prompt"
        return


def known_context_window(conn: Connection) -> int | None:
    """The window the provider can name without I/O, if it can name one.

    That is an explicit setting, whatever the endpoint has already told this
    process, or the bundled table — never a fresh network probe.
    """
    provider = build_provider(conn)
    if provider is None:
        return None
    return provider_context_window(provider)


def build_provider(conn: Connection) -> Provider | None:
    """Construct the provider for ``conn`` (cheap, no I/O); None if unknown."""
    if conn.model is None:
        return None
    try:
        return provider_from_string(
            conn.model, api_key=conn.api_key, base_url=conn.base_url
        )
    except ValueError:
        return None


def _warn_unlisted_model(conn: Connection, out: TextIO) -> None:
    """One warn-only line when the endpoint's ``/models`` omits the model.

    Soft on purpose: gateways often list only part of what they serve (or
    nothing), so an absent id is a hint, not an error — but it catches the
    typo that would otherwise surface as a failure on the first chat message.
    """
    ids = conn.available_models
    if not ids or conn.model is None:
        return
    bare = conn.model.split(":", 1)[-1]
    if conn.model in ids or bare in ids:
        return
    close = difflib.get_close_matches(bare, ids, n=3, cutoff=0.6)
    hint = f" — close: {', '.join(close)}" if close else ""
    print(
        f"  note: the endpoint does not list {bare!r}{hint} (gateways don't "
        "always list every model; continuing)",
        file=out,
    )


def _offer_to_save(
    conn: Connection,
    *,
    input_fn: Callable[[str], str],
    out: TextIO,
    env_sources: Mapping[str, str] | None = None,
    reconfigure: bool = False,
) -> None:
    flavor = conn.flavor
    assert flavor is not None
    env_sources = env_sources or {}
    to_save: dict[str, str] = {}
    if reconfigure:
        # Reconfigure persists the connection as one unit — base URL and key
        # are a pair; saving them piecemeal would assemble mismatched
        # endpoints. The flavor's default base URL is not pinned, and an
        # endpoint-reported window is never persisted (it would go on lying
        # after the deployment is resized).
        if conn.model:
            to_save["LOVIA_MODEL"] = conn.model
        if conn.base_url and conn.base_url != flavor.default_base_url.rstrip("/"):
            to_save[f"{flavor.env_prefix}_BASE_URL"] = conn.base_url
        if conn.api_key:
            to_save[f"{flavor.env_prefix}_API_KEY"] = conn.api_key
        if conn.context_window and conn.context_window_source != "endpoint":
            to_save["LOVIA_CONTEXT_WINDOW"] = str(conn.context_window)
    else:
        if conn.model_source == "prompt" and conn.model:
            to_save["LOVIA_MODEL"] = conn.model
        if conn.base_url_source == "prompt" and conn.base_url:
            to_save[f"{flavor.env_prefix}_BASE_URL"] = conn.base_url
        if conn.api_key_source == "prompt" and conn.api_key:
            to_save[f"{flavor.env_prefix}_API_KEY"] = conn.api_key
        if conn.context_window_source == "prompt" and conn.context_window:
            to_save["LOVIA_CONTEXT_WINDOW"] = str(conn.context_window)
    if not to_save:
        return
    if reconfigure:
        # Default to the layer the connection currently lives in: saving
        # user-level under a live project override would look like the
        # change silently didn't take.
        default = (
            "p"
            if any(env_sources.get(k) == PROJECT_CONFIG_LABEL for k in to_save)
            else "u"
        )
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
            print(
                "  not saved; this configuration applies to this launch only",
                file=out,
            )
            return
        user_scope = True
    save_env_file(to_save, path=user_config_path() if user_scope else config_path())
    label = USER_CONFIG_LABEL if user_scope else f"./{PROJECT_CONFIG_LABEL}"
    print(f"  saved to {label} — owner-only, git-ignored", file=out)
    _warn_shadowed(
        to_save,
        target_label=USER_CONFIG_LABEL if user_scope else PROJECT_CONFIG_LABEL,
        env_sources=env_sources,
        out=out,
    )


def _warn_shadowed(
    saved: Mapping[str, str],
    *,
    target_label: str,
    env_sources: Mapping[str, str],
    out: TextIO,
) -> None:
    """Point out layers that keep beating what was just written.

    Files never override the process environment, and the project file beats
    the user file — a save landing under a live override would otherwise
    look like it silently didn't take.
    """
    for key, value in saved.items():
        current = os.environ.get(key)
        if current is None or current == value:
            continue
        source = env_sources.get(key)  # None -> plain process environment
        if source == target_label:
            continue  # that layer is the one just rewritten
        where = source or "the process environment"
        print(
            f"  note: {key} is currently overridden by {where}; the override "
            "keeps winning until removed",
            file=out,
        )


# ---------------------------------------------------------------- summary -


def mask_key(key: str | None) -> str:
    if not key:
        return "(none)"
    if len(key) > 10:
        return f"{key[:3]}…{key[-4:]}"
    return "…"


def _connection_rows(conn: Connection) -> list[tuple[str, str]]:
    """The four connection values with their sources, for summary and check."""
    if conn.api_key:
        key_cell = f"{mask_key(conn.api_key)} ({conn.api_key_source})"
    else:
        key_cell = "(none — endpoint does not require one)"
    return [
        ("model", f"{conn.model} ({conn.model_source})"),
        ("base URL", f"{conn.base_url} ({conn.base_url_source})"),
        ("api key", key_cell),
        ("context window", _context_window_cell(conn)),
    ]


def run_check(
    conn: Connection,
    *,
    version: str,
    out: TextIO | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """``lovia web --check``: report the resolved connection, probe it, exit.

    Read-only diagnosis — never prompts (no wizard) and never writes. Exit 0
    when the endpoint answered (or can't be verified, which is normal for
    gateways without ``/models``), 2 when configuration is missing or the
    probe failed hard — so CI and containers can assert on it.
    """

    # Resolved at call time, not at import: sys.stdout may be redirected.
    out = out or sys.stdout

    def say(message: str) -> None:
        print(message, file=out)

    say(f"lovia v{version} — configuration check")
    if conn.model is not None:
        for label, value in _connection_rows(conn):
            say(f"  {label:<16} {value}")
    scopes = " · ".join(
        f"{label} ({'present' if path.is_file() else 'absent'})"
        for label, path in (
            (f"./{PROJECT_CONFIG_LABEL}", config_path()),
            (USER_CONFIG_LABEL, user_config_path()),
        )
    )
    say(f"  {'config files':<16} {scopes}")
    missing = conn.missing()
    if missing:
        say(f"  missing: {', '.join(missing)} — {CONFIG_HINT}")
        return 2
    outcome, detail = validate_connection(conn, transport=transport)
    marks = {
        ValidationOutcome.OK: "✓ endpoint reachable",
        ValidationOutcome.AUTH_FAILED: "✗ authentication failed",
        ValidationOutcome.UNREACHABLE: "✗ endpoint unreachable",
        ValidationOutcome.UNVERIFIABLE: "? could not verify the endpoint",
    }
    say(f"  {'probe':<16} {marks[outcome]} ({detail})")
    _warn_unlisted_model(conn, out)
    ok = outcome in (ValidationOutcome.OK, ValidationOutcome.UNVERIFIABLE)
    return 0 if ok else 2


def _context_window_cell(conn: Connection) -> str:
    if conn.context_window is not None:
        return f"{conn.context_window:,} ({conn.context_window_source})"
    known = known_context_window(conn)
    if known is not None:
        return f"auto (provider reports {known:,})"
    return "auto (reactive overflow handling)"


def format_summary(
    conn: Connection,
    *,
    version: str,
    url: str,
    workspace_desc: str,
    db_desc: str,
) -> str:
    """The startup block: what configuration won, and where it came from."""
    rows = _connection_rows(conn) + [
        ("workspace", workspace_desc),
        ("db", db_desc),
    ]
    lines = [f"lovia v{version}"]
    lines += [f"  {label:<16} {value}" for label, value in rows]
    lines += ["", f"serving on {url}"]
    return "\n".join(lines)


def format_app_summary(*, version: str, app_target: str, db_desc: str, url: str) -> str:
    lines = [
        f"lovia v{version}",
        f"  app              {app_target}",
        f"  db               {db_desc}",
        "",
        f"serving on {url}",
    ]
    return "\n".join(lines)
