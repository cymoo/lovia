from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from pydantic import BaseModel

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


def test_function_args_schema_ignores_ctx() -> None:
    def fn(query: str, limit: int = 10, ctx=None) -> str:
        return ""

    schema, names = function_args_schema(fn)
    assert names == ["query", "limit"]
    assert "ctx" not in (schema.get("properties") or {})


def test_validate_args_coerces() -> None:
    def fn(a: int, b: float = 1.5) -> float:
        return a + b

    out = validate_args(fn, {"a": "3"})
    assert out == {"a": 3, "b": 1.5}


def test_coerce_output_to_pydantic() -> None:
    p = coerce_output(Point, {"x": 1, "y": 2})
    assert isinstance(p, Point)
    assert (p.x, p.y) == (1, 2)
