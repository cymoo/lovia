"""Shared typing aliases for dynamic data boundaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypeAlias

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | Mapping[str, "JsonValue"] | Sequence["JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
JsonArray: TypeAlias = list[JsonValue]
JsonSchema: TypeAlias = JsonObject
