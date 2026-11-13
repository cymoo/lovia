"""Settings → MCP: the JSON people paste in, and the JSON they get back.

Most MCP clients write servers as ``{"mcpServers": {name: {...}}}``: a
``command`` with ``args`` and ``env``, or a ``url`` with ``headers``. That is
what the editor takes — the whole document, or just the object inside it —
and what it shows when a server is edited.

``env`` and ``headers`` values are secrets as often as not (API tokens), so
they leave the server masked (``ghp…a1b2``) and come back the way API keys
do: a masked value left as it was keeps what is stored, an empty one
removes the entry, anything else replaces it. A masked value that matches
nothing stored — moved to another key, say — is refused, never saved as
the literal placeholder.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .check import mask_key
from .schema import MCPServerConfig

# What every masked value contains (see ``mask_key``), and no real one does.
MASK_MARK = "…"
_SECRET_FIELDS = ("env", "headers")
_HTTP_TYPES = frozenset(
    {"http", "streamable-http", "streamable_http", "streamableHttp"}
)


def mask(value: str) -> str:
    """A secret as Settings shows it — as an API key is: its ends, or only
    the mark when short. (Values are never empty: saving drops those.)"""
    return mask_key(value)


def parse_servers(doc: Any) -> list[MCPServerConfig]:
    """The servers a pasted document names, validated. Raises ``ValueError``
    with a message fit to show next to the editor."""
    if not isinstance(doc, dict):
        raise ValueError('expected an object such as {"mcpServers": {"name": {...}}}')
    if "command" in doc or "url" in doc:
        raise ValueError('name the server: {"mcpServers": {"name": {"command": ...}}}')
    servers = doc.get("mcpServers", doc)
    if not isinstance(servers, dict) or not servers:
        raise ValueError("no servers in it")
    return [_parse_one(str(name), spec) for name, spec in servers.items()]


def _parse_one(name: str, spec: Any) -> MCPServerConfig:
    if not isinstance(spec, dict):
        raise ValueError(f"{name}: expected an object")
    kind = spec.get("type", spec.get("transport"))
    if kind == "sse":
        raise ValueError(
            f"{name}: the SSE transport is not supported — "
            "use the server's streamable HTTP endpoint (often …/mcp)"
        )
    if kind is not None and kind != "stdio" and kind not in _HTTP_TYPES:
        raise ValueError(f"{name}: unknown type {kind!r}")
    # httpUrl / serverUrl: how two popular clients spell it.
    url = spec.get("url") or spec.get("httpUrl") or spec.get("serverUrl")
    enabled = not spec.get("disabled", False) and spec.get("enabled", True) is not False
    try:
        return MCPServerConfig(
            name=name,
            enabled=bool(enabled),
            command=_text(name, "command", spec.get("command")),
            args=_texts(name, "args", spec.get("args")),
            env=_mapping(name, "env", spec.get("env")),
            url=_text(name, "url", url),
            headers=_mapping(name, "headers", spec.get("headers")),
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        message = str(first.get("msg", "invalid")).removeprefix("Value error, ")
        if where == "name":
            message = (
                "a name is letters, digits, - and _ (at most 32), "
                "starting with a letter or digit"
            )
        raise ValueError(f"{name}: {message}") from None


def _text(name: str, key: str, value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{name}: {key} must be a string")


def _texts(name: str, key: str, value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise ValueError(f"{name}: {key} must be a list of strings")


def _mapping(name: str, key: str, value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name}: {key} must be an object")
    out: dict[str, str] = {}
    for k, v in value.items():
        # A number or a boolean is what was meant as text ("PORT": 3000).
        if isinstance(v, bool):
            out[str(k)] = "true" if v else "false"
        elif isinstance(v, (str, int, float)):
            out[str(k)] = str(v)
        else:
            raise ValueError(f"{name}: {key}.{k} must be a string")
    return out


def spec_of(server: MCPServerConfig, *, masked: bool = True) -> dict[str, Any]:
    """``server`` as one entry of an ``mcpServers`` document."""
    hide = mask if masked else (lambda v: v)
    spec: dict[str, Any] = {}
    if server.command is not None:
        spec["command"] = server.command
        if server.args:
            spec["args"] = list(server.args)
        if server.env:
            spec["env"] = {k: hide(v) for k, v in server.env.items()}
    else:
        spec["type"] = "http"
        spec["url"] = server.url
        if server.headers:
            spec["headers"] = {k: hide(v) for k, v in server.headers.items()}
    if not server.enabled:
        spec["disabled"] = True
    return spec


def restore_secrets(
    new: MCPServerConfig, stored: MCPServerConfig | None
) -> MCPServerConfig:
    """``new`` with its masked values put back from ``stored`` (the server
    it edits, if any), its emptied ones removed. Raises ``ValueError`` for a
    masked value with nothing stored behind it."""
    updates: dict[str, dict[str, str]] = {}
    for field in _SECRET_FIELDS:
        kept: dict[str, str] = getattr(stored, field) if stored is not None else {}
        out: dict[str, str] = {}
        for key, value in getattr(new, field).items():
            if MASK_MARK in value:
                if key in kept and value == mask(kept[key]):
                    out[key] = kept[key]
                    continue
                raise ValueError(
                    f"{new.name}: {field}.{key} is still a masked value — type it in again"
                )
            if value:
                out[key] = value
        updates[field] = out
    return new.model_copy(update=updates)
