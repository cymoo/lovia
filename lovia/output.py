"""Structured output handling.

Two strategies are used:

* If the provider supports JSON Schema natively (currently only OpenAI's
  ``response_format: {type: "json_schema"}``), we forward the schema and parse
  the assistant's final message.
* Otherwise the runner appends :func:`format_output_instructions` to the
  system prompt; the model replies with a JSON document as its final message,
  which is parsed leniently and validated against the schema. Parse failures
  go through the agent's ``output_repair`` policy.

This module keeps the structured-output decision in one place; the runner only
needs to know whether a run has a :class:`StructuredOutput` policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from .types import JsonObject, JsonSchema, JsonValue
from .exceptions import OutputValidationError
from .schema import coerce_output, model_json_schema


class OutputRepairStrategy(Protocol):
    """Decide how to recover from an output validation failure.

    Called after the model produces a final message that doesn't match the
    requested ``output_type``. Return a follow-up *user* prompt the runner
    will append before re-rolling, or ``None`` to give up and re-raise the
    :class:`OutputValidationError`.

    ``attempt`` is 1 on the first failure, 2 after the first repair has
    itself failed, and so on — implementations decide their own cap.
    """

    def build_prompt(self, exc: OutputValidationError, attempt: int) -> str | None: ...


@dataclass
class DefaultOutputRepair:
    """Single-shot English repair prompt — the historical lovia behaviour."""

    max_attempts: int = 1

    def build_prompt(self, exc: OutputValidationError, attempt: int) -> str | None:
        if attempt > self.max_attempts:
            return None
        return (
            "Your previous response could not be parsed into the expected output "
            f"type: {exc}. Please reply again with a response that exactly matches "
            "the required schema. Do not include any explanation, markdown, or "
            "code fences — only the JSON document."
        )


@dataclass
class StructuredOutput:
    """How the runner should obtain a typed final result for one run."""

    output_type: Any
    # True when the provider enforces the schema via ``response_format``;
    # False when the schema is conveyed through the system prompt instead.
    use_native: bool
    # Pre-computed JSON schema for the output type (used by either path).
    schema: JsonSchema


def resolve_structured_output(
    output_type: Any, supports_response_format: bool
) -> StructuredOutput | None:
    """Return the structured-output policy, or ``None`` for plain text."""
    if output_type is str:
        return None
    schema = model_json_schema(output_type)
    return StructuredOutput(
        output_type=output_type,
        use_native=supports_response_format,
        schema=schema,
    )


def format_output_instructions(spec: StructuredOutput) -> str:
    """System-prompt block instructing the model to reply with schema-shaped JSON.

    Used when the provider has no native ``response_format`` support. Lives in
    the system prompt (rather than a synthetic tool description) so the
    requirement stays visible regardless of context length or tool count.
    """
    return (
        "## Output format\n"
        "When you have finished the task, your final reply MUST be a single "
        "JSON document matching this JSON Schema:\n\n"
        f"{json.dumps(spec.schema, ensure_ascii=False)}\n\n"
        "The final reply must contain only the JSON document — no surrounding "
        "explanation, no markdown code fences. You may call tools as needed "
        "before producing it."
    )


def response_format_for(spec: StructuredOutput) -> JsonObject:
    """Build the OpenAI ``response_format`` payload for native JSON Schema."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "output",
            "schema": spec.schema,
            "strict": True,
        },
    }


def parse_structured_output(spec: StructuredOutput, payload: str | JsonValue) -> Any:
    try:
        return coerce_output(spec.output_type, payload)
    except Exception as exc:  # pydantic raises various subclasses
        type_name = getattr(spec.output_type, "__name__", str(spec.output_type))
        raw = payload if isinstance(payload, str) else json.dumps(payload, default=str)
        snippet = (raw[:200] + "…") if len(raw) > 200 else raw
        err = OutputValidationError(
            f"Failed to parse output as {type_name}: {exc}",
            hint=(
                "Set output_repair=True on the Agent (default) to let the model "
                "auto-correct, or pass an OutputRepairStrategy for custom retries."
            ),
            raw=snippet,
            output_type_name=type_name,
        )
        raise err from exc


def loads_lenient(text: str) -> Any:
    """Parse model output as JSON, tolerating common formatting noise.

    Tries, in order: the text as-is, the text with markdown code fences
    stripped, and the first balanced JSON object/array embedded in the text.
    Raises :class:`OutputValidationError` when none of them parse.
    """
    candidates = [text, _strip_code_fences(text)]
    embedded = _extract_json_block(text)
    if embedded is not None:
        candidates.append(embedded)
    last_exc: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_exc = exc
    snippet = (text[:200] + "…") if len(text) > 200 else text
    raise OutputValidationError(
        f"Model output is not valid JSON: {last_exc}",
        hint=(
            "Set output_repair=True on the Agent (default) to let the model "
            "auto-correct, or pass an OutputRepairStrategy for custom retries."
        ),
        raw=snippet,
    ) from last_exc


def _strip_code_fences(text: str) -> str:
    """Remove a single wrapping markdown code fence (```json ... ```)."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline == -1 or not stripped.endswith("```"):
        return stripped
    return stripped[first_newline + 1 : -3].strip()


def _extract_json_block(text: str) -> str | None:
    """Return the first balanced ``{...}`` or ``[...]`` block in ``text``.

    Brace counting ignores braces inside JSON strings, so prose around the
    document doesn't break extraction.
    """
    start = min(
        (idx for idx in (text.find("{"), text.find("[")) if idx != -1),
        default=-1,
    )
    if start == -1:
        return None
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
