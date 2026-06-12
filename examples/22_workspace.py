"""Direct workspace session usage.

Run::

    python examples/22_workspace.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from lovia.workspace import Workspace


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "hello.txt").write_text("hello workspace\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("def foo():\n    return 42\n")

        async with Workspace.local(str(root), mode="trusted").session() as ws:
            session = await ws.open()

            content = await session.read_text("hello.txt")
            print(content.content.strip())

            matches = await session.grep("return", glob="*.py")
            for m in matches:
                print(f"{m.path}:{m.line}: {m.text}")

            result = await session.run("echo $((2 + 3))")
            print(result.stdout.strip())


if __name__ == "__main__":
    asyncio.run(main())
