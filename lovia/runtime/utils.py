"""Small helpers shared by the runner implementation modules."""

from __future__ import annotations

from typing import Any

from ..agent import Agent
from ..messages import Message
from ..providers.base import Provider

_LOG_REPR_MAX = 200


def truncate_repr(value: object, max_len: int = _LOG_REPR_MAX) -> str:
    """One-line log preview of ``value``; the raw value is clipped to ``max_len``."""
    try:
        text = value if isinstance(value, str) else repr(value)
    except Exception:
        text = "<unrepr>"
    # Clip before sanitizing so the replacements scan a bounded slice: this runs
    # on every tool.start/tool.done, even when the level would drop the record.
    overflow = len(text) - max_len
    if overflow > 0:
        text = text[:max_len]
    text = (
        text.replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace("\t", " ")
    )
    return text if overflow <= 0 else f"{text}... <+{overflow} chars>"


def agent_model_label(agent: Agent[Any]) -> str:
    """Best-effort one-line description of the agent's model(s) for logging."""
    model = agent.model
    if isinstance(model, str):
        return model
    if isinstance(model, list):
        labels: list[str] = []
        for model_ref in model:
            if isinstance(model_ref, str):
                # A spec string is already the label; the getattr chain below
                # would fall through to repr() and quote it ("'openai:...'").
                labels.append(model_ref)
                continue
            labels.append(
                str(
                    getattr(model_ref, "model", None)
                    or getattr(model_ref, "name", repr(model_ref))
                )
            )
        return ",".join(labels)
    return getattr(model, "model", None) or getattr(model, "name", None) or repr(model)


def input_preview(user_input: str | list[Message]) -> str:
    """First-line preview of the user input for logging."""
    if isinstance(user_input, str):
        return user_input
    for msg in user_input:
        if msg.role != "system":
            content = msg.content
            return content if isinstance(content, str) else repr(content)
    return ""


def supports_json_schema(providers: list[Provider]) -> bool:
    """Whether every provider in the fallback chain supports ``response_format``.

    The native structured-output path is only safe when *all* providers in
    the chain accept the schema payload — a fallback provider that doesn't
    would reject the request mid-run.
    """
    return bool(providers) and all(
        bool(getattr(p, "supports_json_schema", False)) for p in providers
    )
