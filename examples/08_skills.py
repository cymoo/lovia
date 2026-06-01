"""File-system skills.

Each subdirectory under ``./skills`` with a ``SKILL.md`` (with YAML
frontmatter ``name`` + ``description``) becomes an entry the model can lazily
load via the ``load_skill`` tool. The catalog is rendered into the system
prompt so the model knows what's available.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from lovia import Agent, Runner
from lovia.skills import SkillCatalog

from dotenv import load_dotenv

load_dotenv()

SKILL_BODY = """---
name: refund-policy
description: Company policy on issuing refunds.
---
# Refund Policy

- Full refunds within 14 days.
- Pro-rated refunds otherwise.
- Always be polite.
"""


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "skills"
        (root / "refund-policy").mkdir(parents=True)
        (root / "refund-policy" / "SKILL.md").write_text(SKILL_BODY)

        agent = Agent(
            name="SupportBot",
            instructions="Help the customer. Load skills when relevant.",
            model=os.getenv("OPENAI_DEFAULT_MODEL", "openai:gpt-5.4"),
            skills=SkillCatalog.from_dir(root),
        )
        result = await Runner.run(agent, "Can I get a refund 5 days after purchase?")
        print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
