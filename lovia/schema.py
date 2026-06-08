"""Type -> JSON Schema conversion.

The framework accepts four flavours of "schema" from users:

* a :class:`pydantic.BaseModel` subclass,
* a :func:`dataclasses.dataclass`,
* a :class:`typing.TypedDict`,
* a plain function whose parameters carry type hints.

This module normalizes all of them into a JSON Schema dict suitable for OpenAI
Chat Completions ``tools`` and ``response_format`` payloads. We rely on
pydantic for the heavy lifting (it already handles dataclasses, TypedDicts,
and primitive type adapters), which keeps this file short while staying
correct on tricky cases (``Optional``, ``Literal``, ``Union``, nested models).

Parameter annotations may be wrapped in :data:`typing.Annotated` to carry
extra metadata. Two forms are recognised:

* ``Annotated[T, pydantic.Field(description=..., ge=..., ...)]`` — pydantic
  handles the JSON Schema enrichment automatically.
* ``Annotated[T, "free-text description"]`` — bare strings are converted to
  ``Field(description=...)`` so users don't need to import pydantic.
"""

from __future__ import annotations

import inspect
from typing import Annotated, Callable, cast, get_args, get_origin, get_type_hints

from pydantic import BaseModel, Field, TypeAdapter, create_model

from ._types import JsonObject, JsonSchema


def _is_context_annotation(annotation: object) -> bool:
    """True if ``annotation`` is ``RunContext`` or ``RunContext[X]``.

    Imported lazily to avoid a circular import (``run_context`` does not
    depend on this module, but conceptually it lives "above" schema).
    """
    from .run_context import RunContext

    # Unwrap Annotated[T, ...] for the context check; the marker matters,
    # not the metadata.
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin = get_origin(annotation) or annotation
    return origin is RunContext


def _normalize_annotation(annotation: object) -> object:
    """Convert ``Annotated[T, "desc"]`` to ``Annotated[T, Field(description=...)]``.

    Bare string metadata is treated as a description so users can write
    ``query: Annotated[str, "the search query"]`` without importing pydantic.
    Existing ``Field(...)`` metadata is left intact.
    """
    if get_origin(annotation) is not Annotated:
        return annotation
    base, *meta = get_args(annotation)
    new_meta: list[object] = []
    for item in meta:
        if isinstance(item, str):
            new_meta.append(Field(description=item))
        else:
            new_meta.append(item)
    return Annotated[(base, *new_meta)]  # type: ignore[valid-type]


def _iter_arg_params(
    fn: Callable[..., object],
) -> list[tuple[str, inspect.Parameter, object]]:
    """Yield ``(name, param, annotation)`` for each LLM-visible parameter.

    Skips ``self``/``cls``, underscore-prefixed names, var-args, and any
    parameter annotated as :class:`RunContext` (those are runner-injected).
    """
    sig = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)
    out: list[tuple[str, inspect.Parameter, object]] = []
    for name, param in sig.parameters.items():
        if name.startswith("_") or name in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        annotation = hints.get(name, str)
        if _is_context_annotation(annotation):
            continue
        out.append((name, param, _normalize_annotation(annotation)))
    return out


def model_json_schema(tp: object) -> JsonSchema:
    """Return a JSON Schema for an arbitrary supported type ``tp``."""
    # Pydantic BaseModel subclass - use its built-in schema.
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return _strip_titles(tp.model_json_schema())

    # Everything else goes through TypeAdapter, which handles dataclasses,
    # TypedDicts, primitives, generics, unions, etc.
    return _strip_titles(TypeAdapter(tp).json_schema())


def function_args_schema(
    fn: Callable[..., object],
    *,
    strict: bool = False,
) -> tuple[JsonSchema, list[str]]:
    """Build a JSON Schema for a function's keyword arguments.

    Returns ``(schema, param_names)`` so the runner can validate inputs and
    map them back to call arguments.

    Parameters whose name starts with an underscore, or that are annotated as
    :class:`RunContext` (which the runner injects), are excluded from the
    schema. When ``strict=True`` the resulting object enforces
    ``additionalProperties: False`` and marks every field as required
    (matching OpenAI's strict-mode requirements).
    """
    fields: dict[str, object] = {}
    param_names: list[str] = []
    for name, param, annotation in _iter_arg_params(fn):
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, default)
        param_names.append(name)

    if not fields:
        # OpenAI requires an object schema even when the function takes no args.
        return {"type": "object", "properties": {}, "additionalProperties": False}, []

    # ``create_model`` gives us a one-off pydantic model whose JSON Schema is
    # exactly what we want for the tool's ``parameters`` field.
    Model = create_model(f"{fn.__name__.title()}Args", **fields)  # type: ignore[call-overload]
    schema = _strip_titles(Model.model_json_schema())
    schema.setdefault("additionalProperties", False)
    if strict:
        schema["additionalProperties"] = False
        schema["required"] = list(param_names)
    return schema, param_names


def validate_args(fn: Callable[..., object], data: JsonObject) -> dict[str, object]:
    """Validate ``data`` against ``fn``'s signature and coerce types.

    Uses pydantic so that e.g. ``"3"`` becomes ``3`` when the annotation is
    ``int``. Returns the cleaned kwargs dict.
    """
    fields: dict[str, object] = {}
    for name, param, annotation in _iter_arg_params(fn):
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, default)
    if not fields:
        return {}
    Model = create_model(f"{fn.__name__.title()}Args", **fields)  # type: ignore[call-overload]
    return Model(**data).model_dump()


def coerce_output(tp: object, data: object) -> object:
    """Coerce ``data`` (usually parsed JSON) into ``tp`` via pydantic."""
    if tp is str:
        return data if isinstance(data, str) else str(data)
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if isinstance(data, str):
            return tp.model_validate_json(data)
        return tp.model_validate(data)
    if isinstance(data, str):
        return TypeAdapter(tp).validate_json(data)
    return TypeAdapter(tp).validate_python(data)


def _strip_titles(schema: JsonSchema) -> JsonSchema:
    """Remove pydantic's auto-generated ``title`` fields.

    They are valid JSON Schema but add noise to the system prompt and tool
    payloads. Strip them recursively for a cleaner wire format.
    """
    if isinstance(schema, dict):
        schema.pop("title", None)
        for value in schema.values():
            if isinstance(value, dict):
                _strip_titles(cast(JsonObject, value))
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _strip_titles(cast(JsonObject, item))
    return schema
