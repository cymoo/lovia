"""Structured output handling.

Two strategies are used:

* If the provider supports JSON Schema natively (currently only OpenAI's
  ``response_format: {type: "json_schema"}``), we forward the schema and parse
  the assistant's final message.
* Otherwise the runner installs a synthetic ``final_output`` tool whose
  parameters match the requested type; the model is instructed to call it
  exactly once, and its arguments become the structured output.

This module keeps the structured-output decision in one place; the runner only
needs to know whether a run has a ``StructuredOutput`` policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from .exceptions import OutputValidationError
from .schema import coerce_output, model_json_schema


FINAL_OUTPUT_TOOL_NAME = "final_output"


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

    output_type: type
    # When True, ``final_output`` is injected as a tool the model must call.
    use_tool_fallback: bool
    # Pre-computed JSON schema for the output type (used by either path).
    schema: dict[str, Any]


def resolve_structured_output(
    output_type: type, supports_response_format: bool
) -> StructuredOutput | None:
    """Return the structured-output policy, or ``None`` for plain text."""
    if output_type is str:
        return None
    schema = model_json_schema(output_type)
    return StructuredOutput(
        output_type=output_type,
        use_tool_fallback=not supports_response_format,
        schema=schema,
    )


def response_format_for(spec: StructuredOutput) -> dict[str, Any]:
    """Build the OpenAI ``response_format`` payload for native JSON Schema."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "output",
            "schema": spec.schema,
            "strict": True,
        },
    }


def parse_structured_output(
    spec: StructuredOutput, payload: str | dict[str, Any]
) -> Any:
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
    """Parse JSON but raise :class:`OutputValidationError` on failure."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        snippet = (text[:200] + "…") if len(text) > 200 else text
        raise OutputValidationError(
            f"Model output is not valid JSON: {exc}",
            hint=(
                "Set output_repair=True on the Agent (default) to let the model "
                "auto-correct, or pass an OutputRepairStrategy for custom retries."
            ),
            raw=snippet,
        ) from exc
