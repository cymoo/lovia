"""Skill catalog: file-backed prompt fragments loaded on demand.

A "skill" is a directory containing ``SKILL.md`` with YAML-style frontmatter::

    ---
    name: refund-policy
    description: How to handle customer refund requests.
    ---
    # Refund Policy
    ...

The agent's system prompt advertises the catalog as ``name: description``
pairs. The model uses the ``load_skill(name)`` tool to pull in the full
contents when relevant. This matches the Claude Code / Anthropic Skills
convention and keeps the system prompt small.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .tools import Tool


@dataclass
class Skill:
    name: str
    description: str
    content: str


class SkillCatalog:
    """A collection of skills resolved from a directory of ``SKILL.md`` files."""

    def __init__(self, skills: list[Skill]) -> None:
        self._skills = {s.name: s for s in skills}

    @classmethod
    def from_dir(cls, path: str | Path) -> "SkillCatalog":
        """Scan ``path`` for ``*/SKILL.md`` and build a catalog."""
        root = Path(path)
        skills: list[Skill] = []
        if not root.exists():
            return cls(skills)
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "SKILL.md"
            if not manifest.exists():
                continue
            raw = manifest.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            skills.append(
                Skill(
                    name=meta.get("name", entry.name),
                    description=meta.get(
                        "description", body.splitlines()[0] if body else ""
                    ),
                    content=raw,
                )
            )
        return cls(skills)

    def names(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def render_catalog(self) -> str:
        """Return a system-prompt fragment listing every skill."""
        if not self._skills:
            return ""
        lines = ["Available skills (call load_skill to read them in full):"]
        for s in self._skills.values():
            lines.append(f"- {s.name}: {s.description}")
        return "\n".join(lines)

    def tools(self) -> list[Tool]:
        """Return the tools that expose this catalog to the model."""

        # Bound here so the closure captures the catalog instance.
        async def load_skill(name: str) -> str:
            skill = self.get(name)
            if skill is None:
                return f"Unknown skill: {name}. Available: {', '.join(self.names()) or '(none)'}"
            return skill.content

        async def list_skills() -> str:
            return self.render_catalog() or "(no skills)"

        from .tools import tool as _tool

        # We use the @tool decorator programmatically to reuse its schema
        # generation logic.
        load_skill.__doc__ = "Load the full contents of a named skill."
        list_skills.__doc__ = "List all available skills with descriptions."
        return [_tool(load_skill), _tool(list_skills)]


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse a leading ``---`` block of ``key: value`` lines from ``text``."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, parts[2].strip()
