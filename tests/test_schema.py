from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from lovia import RunContext
from lovia.schema import (
    coerce_output,
    function_args_schema,
    model_json_schema,
    validate_args,
)


class Point(BaseModel):
    x: int
    y: int


@dataclass
class Range:
    lo: int
    hi: int


class CityWeather(TypedDict):
    city: str
    temp_c: float


def test_pydantic_schema() -> None:
    s = model_json_schema(Point)
    assert s["type"] == "object"
    assert set(s["required"]) == {"x", "y"}


def test_dataclass_schema() -> None:
    s = model_json_schema(Range)
    assert s["type"] == "object"
    assert "lo" in s["properties"] and "hi" in s["properties"]


def test_typeddict_schema() -> None:
    s = model_json_schema(CityWeather)
    assert s["type"] == "object"
    assert s["properties"]["temp_c"]["type"] == "number"


def test_function_args_schema_ignores_run_context() -> None:
    def fn(ctx: RunContext, query: str, limit: int = 10) -> str:
        return ""

    schema, names = function_args_schema(fn)
    assert names == ["query", "limit"]
    assert "ctx" not in (schema.get("properties") or {})

    # The exclusion is annotation-based: a param named "ctx" but NOT typed
    # as RunContext is treated as a normal LLM-supplied arg.
    def fn2(query: str, ctx: str = "") -> str:
        return ""

    schema2, names2 = function_args_schema(fn2)
    assert names2 == ["query", "ctx"]


def test_optional_annotated_keeps_field_metadata() -> None:
    # Python 3.10's get_type_hints wraps an `ann = None` param in Optional[...],
    # burying the Annotated Field inside a union member — which used to drop
    # its description and constraints from the schema (silently, and on 3.10
    # only). The schema must come out identical on every supported version.
    def fn(
        x: Annotated[int | None, Field(default=None, ge=1, description="the x")] = None,
    ) -> None:
        return None

    schema, _ = function_args_schema(fn)
    assert schema["properties"]["x"] == {
        "anyOf": [{"minimum": 1, "type": "integer"}, {"type": "null"}],
        "default": None,
        "description": "the x",
    }


def test_user_written_optional_around_annotated() -> None:
    # The hoist also normalizes an explicit Optional[Annotated[...]]: the
    # metadata applies to the whole optional value, on every version.
    def fn(name: Optional[Annotated[str, Field(description="who")]] = None) -> None:
        return None

    schema, _ = function_args_schema(fn)
    prop = schema["properties"]["name"]
    assert prop["description"] == "who"
    assert {"type": "null"} in prop["anyOf"]


def test_validate_args_coerces() -> None:
    def fn(a: int, b: float = 1.5) -> float:
        return a + b

    out = validate_args(fn, {"a": "3"})
    assert out == {"a": 3, "b": 1.5}


def test_coerce_output_to_pydantic() -> None:
    p = coerce_output(Point, {"x": 1, "y": 2})
    assert isinstance(p, Point)
    assert (p.x, p.y) == (1, 2)


def test_schema_preserves_user_field_named_title() -> None:
    # Pydantic's auto-generated ``title`` annotations are stripped, but a
    # *field* named ``title`` (a dict under ``properties``) must survive.
    class Doc(BaseModel):
        title: str
        body: str

    s = model_json_schema(Doc)
    assert s["properties"]["title"] == {"type": "string"}
    assert set(s["required"]) == {"title", "body"}
    assert "title" not in s  # the schema-level annotation is still stripped


def test_validate_args_keeps_nested_model_instances() -> None:
    def fn(point: Point, scale: int = 1) -> str:
        return ""

    out = validate_args(fn, {"point": {"x": 1, "y": 2}, "scale": "3"})
    assert isinstance(out["point"], Point)
    assert (out["point"].x, out["point"].y) == (1, 2)
    assert out["scale"] == 3
