"""Autonomous Agent.

An LLM dynamically selects and invokes tools in a loop, using the results to
decide its next action. The agent continues until it has completed the task
or a stopping condition is met (max_turns).

This pattern is appropriate for open-ended tasks where:
  • The number of required steps cannot be known in advance.
  • The agent must adapt based on real environmental feedback (tool outputs).

Demo: A coding-assistant agent that can read files, run Python snippets,
and search a mock knowledge base to diagnose and fix a bug.

Reference:
  https://www.anthropic.com/engineering/building-effective-agents#agents

Run::

    python examples/workflows/06_autonomous_agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import textwrap

from lovia import Agent, Runner, tool
from dotenv import load_dotenv

load_dotenv()

MODEL = os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4")


# ---------------------------------------------------------------------------
# Mock file system (in-memory)
# ---------------------------------------------------------------------------

_FILES: dict[str, str] = {
    "utils.py": textwrap.dedent("""\
        def calculate_average(numbers):
            total = 0
            for n in numbers:
                total += n
            return total / len(numbers)  # bug: ZeroDivisionError when list is empty
    """),
    "test_utils.py": textwrap.dedent("""\
        from utils import calculate_average

        def test_normal():
            assert calculate_average([1, 2, 3]) == 2.0

        def test_empty():
            # This test currently fails with ZeroDivisionError
            result = calculate_average([])
            assert result == 0.0
    """),
}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool
def list_files() -> str:
    """List all files available in the project."""
    return json.dumps(list(_FILES.keys()))


@tool
def read_file(path: str) -> str:
    """Read the content of a project file by its path."""
    if path not in _FILES:
        return f"Error: file {path!r} not found. Available: {list(_FILES.keys())}"
    return _FILES[path]


@tool
def write_file(path: str, content: str) -> str:
    """Write (create or overwrite) a project file with new content."""
    _FILES[path] = content
    return f"File {path!r} written successfully ({len(content)} chars)."


@tool
def run_python(code: str) -> str:
    """Execute a Python snippet and return its stdout/stderr output.

    The snippet runs in an isolated subprocess. Use print() to emit results.
    Project files are not accessible here — paste relevant code inline.
    """
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
        f.write(code)
        tmp_path = f.name
    try:
        proc = subprocess.run(
            ["python", tmp_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = proc.stdout + proc.stderr
        return output.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: execution timed out (10 s)"
    finally:
        os.unlink(tmp_path)


@tool
def search_knowledge_base(query: str) -> str:
    """Search a mock Python best-practices knowledge base.

    Returns relevant guidance for the given query.
    """
    kb = {
        "empty list": (
            "When a function receives an empty collection, validate the input early. "
            "Return a sensible default (e.g. 0, None) or raise a descriptive ValueError."
        ),
        "zero division": (
            "Guard against division by zero with an explicit check: "
            "`if not denominator: return default_value` before the division."
        ),
        "average": (
            "For a safe average function: check if the sequence is empty, "
            "return 0 (or raise ValueError) in that case, otherwise return sum(seq)/len(seq)."
        ),
    }
    query_lower = query.lower()
    results = [v for k, v in kb.items() if k in query_lower or query_lower in k]
    return "\n".join(results) if results else "No relevant entries found."


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

coding_agent = Agent(
    name="CodingAgent",
    instructions=(
        "You are an expert Python debugging assistant. Your goal is to:\n"
        "1. Understand the bug described by the user.\n"
        "2. Read the relevant source files.\n"
        "3. Consult the knowledge base if needed.\n"
        "4. Run experiments with run_python to verify your understanding.\n"
        "5. Write the fixed code back to the file.\n"
        "6. Verify the fix by running the test code.\n"
        "7. Report your findings and the fix applied.\n\n"
        "Be methodical: gather information before acting, verify assumptions."
    ),
    model=MODEL,
    tools=[list_files, read_file, write_file, run_python, search_knowledge_base],
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    task = (
        "There is a bug in utils.py. The function calculate_average crashes with "
        "a ZeroDivisionError when given an empty list. "
        "Please investigate, fix the bug, and confirm the fix passes the tests in test_utils.py."
    )

    print("Task:", task)
    print("=" * 60)

    result = await Runner.run(coding_agent, task, max_turns=15)

    print("\n--- Agent Report ---")
    print(result.output)
    print(f"\n[turns={result.turns}, usage={result.usage}]")

    print("\n--- Final utils.py ---")
    print(_FILES.get("utils.py", "(not found)"))


if __name__ == "__main__":
    asyncio.run(main())
