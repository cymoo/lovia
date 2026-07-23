"""First-run onboarding for the lovia web CLI.

Resolves the model connection (model id, base URL, API key, context window)
from flags and environment layers with per-value source tracking, prompts
interactively for whatever is missing, validates freshly entered values
against the endpoint, and offers to persist them to ``.lovia/config.env``
(owner-only, git-ignored) — auto-loaded on the next launch at the lowest
precedence: flag > environment > ``.lovia/config.env`` (or a legacy ``./.env``).

Everything here is CLI-only: the embeddable core (``create_app`` / ``serve``)
never loads env files, so an app embedding lovia brings its own config.
"""

from __future__ import annotations

import enum
import getpass as _getpass
import logging
import os
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
CONFIG_HINT = "pass --model and --api-key, or set LOVIA_MODEL and OPENAI_API_KEY"

# Saved config lives beside the chat DB under the CWD-relative .lovia/ dir — a
# lovia-owned home, so it never collides with a generic ./.env another tool
# might read, and (unlike a bare ~/.env) it stays tidy when launched from $HOME.
CONFIG_DIR = Path(".lovia")


def config_path() -> Path:
    """Default path the wizard saves resolved config to (``.lovia/config.env``)."""
    return CONFIG_DIR / "config.env"

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
        return ValidationOutcome.OK, f"HTTP {response.status_code}"
    return ValidationOutcome.UNVERIFIABLE, f"HTTP {response.status_code}"


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
    """Create lovia's data dir and drop a ``*`` .gitignore so its contents —
    saved secrets and the chat DB — are never committed inside someone's repo."""
    directory.mkdir(parents=True, exist_ok=True)
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "# lovia data — secrets and chat history; never commit.\n*\n",
            encoding="utf-8",
        )


def save_env_file(values: Mapping[str, str], path: Path | None = None) -> Path:
    """Append plain ``KEY=value`` lines to ``.lovia/config.env`` (created if missing).

    Deliberately append-only: no dedup or rewrite — python-dotenv's
    last-occurrence-wins parsing makes an appended value effective, and a key
    already loaded is never offered for saving again. The file holds API keys,
    so it's written owner-only (``0600``); when saving to the default location
    the ``.lovia/`` dir is also git-ignored.
    """
    default = path is None
    path = path or config_path()
    if default:
        _protect_config_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    # Patch a missing trailing newline so we never glue onto the last line.
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(prefix + "".join(f"{key}={value}\n" for key, value in values.items()))
    with suppress(OSError):  # best-effort; Windows ignores mode bits
        path.chmod(0o600)
    return path


# ------------------------------------------------------------ interaction -


def interactive_setup(
    conn: Connection,
    *,
    env_sources: Mapping[str, str],
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = _getpass.getpass,
    transport: httpx.BaseTransport | None = None,
    out: TextIO = sys.stdout,
) -> Connection:
    """Ask for the missing connection values, validate, offer to persist.

    Only items no configuration channel supplied are asked. Raises
    :class:`UserError` when stdin closes mid-prompt; ``KeyboardInterrupt``
    propagates so the CLI's existing handler can exit with 130.
    """
    try:
        return _run_wizard(
            conn,
            env_sources=env_sources,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
            transport=transport,
            out=out,
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
) -> Connection:
    def say(message: str) -> None:
        print(message, file=out)

    say("")
    say("lovia needs a model endpoint to serve the web UI — answering here")
    say("takes a few seconds, and can be saved so you never retype it.")
    say(f"(non-interactive alternatives: {CONFIG_HINT})")
    say("")

    if conn.model is None:
        say("  examples: openai:gpt-5.5 · anthropic:claude-sonnet-4-5")
        say("            deepseek-v4-pro (bare name = any OpenAI-compatible endpoint)")
        while not conn.model:
            conn.model = input_fn("  Model: ").strip() or None
        conn.model_source = "prompt"
        # The flavor is known only now: pull in its env defaults before
        # deciding what else to ask.
        _derive_endpoint(conn, env_sources)

    flavor = conn.flavor
    assert flavor is not None

    if conn.base_url_source == "default":
        answer = input_fn(f"  Base URL [{conn.base_url}]: ").strip()
        if answer:
            conn.base_url = answer.rstrip("/")
        conn.base_url_source = "prompt"

    if conn.api_key is None:
        _prompt_api_key(conn, getpass_fn=getpass_fn, out=out)

    _validation_loop(
        conn, input_fn=input_fn, getpass_fn=getpass_fn, transport=transport, out=out
    )
    _maybe_prompt_context_window(conn, input_fn=input_fn, out=out)
    _offer_to_save(conn, input_fn=input_fn, out=out)
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
        prompt = "  API key (input hidden; required for this endpoint): "
    else:
        prompt = "  API key (input hidden; Enter to skip if the endpoint needs none): "
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
        "  the provider does not report this model's context window; without"
        " it, long chats fall back to reactive overflow handling",
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


def _offer_to_save(
    conn: Connection, *, input_fn: Callable[[str], str], out: TextIO
) -> None:
    flavor = conn.flavor
    assert flavor is not None
    to_save: dict[str, str] = {}
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
    answer = input_fn(f"  Save to {config_path()} for next launches? [Y/n]: ")
    if answer.strip().lower() in ("", "y", "yes"):
        saved = save_env_file(to_save)
        print(f"  saved to {saved} — owner-only, git-ignored", file=out)
    else:
        print("  not saved; this configuration applies to this launch only", file=out)


# ---------------------------------------------------------------- summary -


def mask_key(key: str | None) -> str:
    if not key:
        return "(none)"
    if len(key) > 10:
        return f"{key[:3]}…{key[-4:]}"
    return "…"


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
    host: str,
    port: int,
    workspace_desc: str,
    db_desc: str,
) -> str:
    """The startup block: what configuration won, and where it came from."""
    if conn.api_key:
        key_cell = f"{mask_key(conn.api_key)} ({conn.api_key_source})"
    else:
        key_cell = "(none — endpoint does not require one)"
    rows = [
        ("model", f"{conn.model} ({conn.model_source})"),
        ("base URL", f"{conn.base_url} ({conn.base_url_source})"),
        ("api key", key_cell),
        ("context window", _context_window_cell(conn)),
        ("workspace", workspace_desc),
        ("db", db_desc),
    ]
    lines = [f"lovia v{version}"]
    lines += [f"  {label:<16} {value}" for label, value in rows]
    lines += ["", f"serving on http://{host}:{port}"]
    return "\n".join(lines)


def format_app_summary(
    *, version: str, app_target: str, db_desc: str, host: str, port: int
) -> str:
    lines = [
        f"lovia v{version}",
        f"  app              {app_target}",
        f"  db               {db_desc}",
        "",
        f"serving on http://{host}:{port}",
    ]
    return "\n".join(lines)
