"""Local sandbox tools.

Run::

    python examples/22_sandbox.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from lovia.sandbox import Sandbox


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "hello.txt").write_text("hello sandbox\n", encoding="utf-8")

        async with Sandbox.local(str(root), mode="trusted").session() as sandbox:
            content = await sandbox.read_text("hello.txt")
            print(content.content.strip())

            result = await sandbox.run("python -c 'print(2 + 3)'")
            print(result.stdout.strip())


if __name__ == "__main__":
    asyncio.run(main())
