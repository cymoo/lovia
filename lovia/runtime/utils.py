"""Small helpers shared by the runner implementation modules."""

from __future__ import annotations

from ..agent import Agent
from ..messages import Message

_LOG_REPR_MAX = 200


def truncate_repr(value: object, max_len: int = _LOG_REPR_MAX) -> str:
    """Render ``value`` for a single log line, clipping to ``max_len`` chars."""
    try:
        text = value if isinstance(value, str) else repr(value)
    except Exception:
        text = "<unrepr>"
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... <+{len(text) - max_len} chars>"


def agent_model_label(agent: Agent) -> str:
    """Best-effort one-line description of the agent's model(s) for logging."""
    model = agent.model
    if isinstance(model, str):
        return model
    if isinstance(model, list):
        labels: list[str] = []
        for model_ref in model:
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


def supports_json_schema(agent: Agent) -> bool:
    """Whether the agent's provider can use OpenAI-style ``response_format``."""
    provider = agent.resolve_provider() if isinstance(agent.model, str) else agent.model
    if isinstance(provider, list):
        return all(bool(getattr(p, "supports_json_schema", False)) for p in provider)
    return bool(getattr(provider, "supports_json_schema", False))
