"""Smoke-import the sandbox examples so API drift fails CI without an LLM key.

We can't *run* the LLM-driven examples (they need network + an API key),
but a plain ``import`` exercises every type and Session-shape we wire up,
which is exactly the class of bug ("Protocols cannot be instantiated",
missing exports, …) that has bitten us.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize(
    "name",
    [
        "22_sandbox",
        "23_sandbox_session",
        "24_custom_sandbox",
    ],
)
def test_example_module_imports(name: str) -> None:
    """Importing the module must not raise — exercises type-level wiring."""
    path = EXAMPLES / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_lovia_example_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
