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
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, get_origin, get_type_hints

from pydantic import BaseModel, TypeAdapter, create_model


def _is_context_annotation(annotation: Any) -> bool:
    """True if ``annotation`` is ``RunContext`` or ``RunContext[X]``.

    Imported lazily to avoid a circular import (``run_context`` does not
    depend on this module, but conceptually it lives "above" schema).
    """
    from .run_context import RunContext

    origin = get_origin(annotation) or annotation
    return origin is RunContext


def _iter_arg_params(
    fn: Callable[..., Any],
) -> list[tuple[str, inspect.Parameter, Any]]:
    """Yield ``(name, param, annotation)`` for each LLM-visible parameter.

    Skips ``self``/``cls``, underscore-prefixed names, var-args, and any
    parameter annotated as :class:`RunContext` (those are runner-injected).
    """
    sig = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=False)
    out: list[tuple[str, inspect.Parameter, Any]] = []
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
        out.append((name, param, annotation))
    return out


def model_json_schema(tp: Any) -> dict[str, Any]:
    """Return a JSON Schema for an arbitrary supported type ``tp``."""
    # Pydantic BaseModel subclass - use its built-in schema.
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return _strip_titles(tp.model_json_schema())

    # Everything else goes through TypeAdapter, which handles dataclasses,
    # TypedDicts, primitives, generics, unions, etc.
    return _strip_titles(TypeAdapter(tp).json_schema())


def function_args_schema(fn: Callable[..., Any]) -> tuple[dict[str, Any], list[str]]:
    """Build a JSON Schema for a function's keyword arguments.

    Returns ``(schema, param_names)`` so the runner can validate inputs and
    map them back to call arguments.

    Parameters whose name starts with an underscore, or that are annotated as
    :class:`RunContext` (which the runner injects), are excluded from the
    schema.
    """
    fields: dict[str, Any] = {}
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
    return schema, param_names


def validate_args(fn: Callable[..., Any], data: dict[str, Any]) -> dict[str, Any]:
    """Validate ``data`` against ``fn``'s signature and coerce types.

    Uses pydantic so that e.g. ``"3"`` becomes ``3`` when the annotation is
    ``int``. Returns the cleaned kwargs dict.
    """
    fields: dict[str, Any] = {}
    for name, param, annotation in _iter_arg_params(fn):
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, default)
    if not fields:
        return {}
    Model = create_model(f"{fn.__name__.title()}Args", **fields)  # type: ignore[call-overload]
    return Model(**data).model_dump()


def coerce_output(tp: Any, data: Any) -> Any:
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


def _strip_titles(schema: dict[str, Any]) -> dict[str, Any]:
    """Remove pydantic's auto-generated ``title`` fields.

    They are valid JSON Schema but add noise to the system prompt and tool
    payloads. Strip them recursively for a cleaner wire format.
    """
    if isinstance(schema, dict):
        schema.pop("title", None)
        for value in schema.values():
            if isinstance(value, dict):
                _strip_titles(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _strip_titles(item)
    return schema
