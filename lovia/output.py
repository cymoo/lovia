"""Structured output handling.

Two strategies are used:

* If the provider supports JSON Schema natively (currently only OpenAI's
  ``response_format: {type: "json_schema"}``), we forward the schema and parse
  the assistant's final message.
* Otherwise the runner installs a synthetic ``final_output`` tool whose
  parameters match the requested type; the model is instructed to call it
  exactly once, and its arguments become the structured output.

This module exposes helpers for both strategies; the runner picks one in
``_resolve_output_strategy``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .exceptions import OutputValidationError
from .schema import coerce_output, model_json_schema


FINAL_OUTPUT_TOOL_NAME = "final_output"


@dataclass
class OutputSpec:
    """How the runner should obtain structured output for a given run."""

    output_type: type
    # When True, ``final_output`` is injected as a tool the model must call.
    use_tool_fallback: bool
    # Pre-computed JSON schema for the output type (used by either path).
    schema: dict[str, Any]


def build_output_spec(
    output_type: type, supports_response_format: bool
) -> OutputSpec | None:
    """Return an :class:`OutputSpec` or ``None`` for plain text output."""
    if output_type is str:
        return None
    schema = model_json_schema(output_type)
    return OutputSpec(
        output_type=output_type,
        use_tool_fallback=not supports_response_format,
        schema=schema,
    )


def response_format_for(spec: OutputSpec) -> dict[str, Any]:
    """Build the OpenAI ``response_format`` payload for native JSON Schema."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "output",
            "schema": spec.schema,
            "strict": True,
        },
    }


def final_output_tool_schema(spec: OutputSpec) -> dict[str, Any]:
    """Build a tool schema describing the synthetic ``final_output`` tool."""
    return {
        "type": "function",
        "function": {
            "name": FINAL_OUTPUT_TOOL_NAME,
            "description": "Call this once with the final structured answer to complete the task.",
            "parameters": spec.schema,
        },
    }


def parse_output(spec: OutputSpec, payload: str | dict[str, Any]) -> Any:
    try:
        return coerce_output(spec.output_type, payload)
    except Exception as exc:  # pydantic raises various subclasses
        raise OutputValidationError(
            f"Failed to parse output as {spec.output_type.__name__}: {exc}"
        ) from exc


def loads_lenient(text: str) -> Any:
    """Parse JSON but raise :class:`OutputValidationError` on failure."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OutputValidationError(f"Model output is not valid JSON: {exc}") from exc
