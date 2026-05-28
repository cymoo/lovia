"""Skill catalog: file-backed prompt fragments loaded on demand.

A *skill* is a directory containing ``SKILL.md`` with YAML-style frontmatter::

    ---
    name: refund-policy
    description: How to handle customer refund requests.
    triggers: [refund, money back, return]
    ---
    # Refund Policy
    ...

Optional sibling subdirectories — discovered but **not** auto-loaded — let
authors keep the entry-point lean while leaving room for richer assets:

* ``references/`` — supplementary docs the model can pull as needed.
* ``scripts/``    — runnable snippets (executed by an external tool, e.g.
  the optional ``lovia.builtins.shell`` / ``code`` workers).
* ``assets/``     — templates, fixtures, etc.

Two catalog modes:

* ``mode="lazy"`` (default) — only ``name + description + path`` lands in the
  system prompt. The model calls ``load_skill(name)`` to pull a ``SKILL.md``,
  and ``read_skill_file(name, relpath)`` to pull a specific sub-file.
* ``mode="eager"`` — every ``SKILL.md`` body is inlined into the system
  prompt up front. Suited to a small set of always-relevant skills.

Both modes inject a short *usage rules* paragraph telling the model how to
trigger and consume skills (including the ``$SkillName`` shortcut).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .exceptions import ToolError
from .tools import Tool


SkillMode = Literal["lazy", "eager"]


_USAGE_RULES_LAZY = """\
### How to use skills
- Discovery: the list above is the index of skills available this session
  (name + description + path). Bodies are loaded on demand.
- Trigger: if the user names a skill (with `$SkillName` or by plain match)
  or the task clearly fits a skill's description, use that skill for the
  turn. Multiple mentions mean use all of them.
- Workflow: call `load_skill(name)` first, follow only what's needed in
  `SKILL.md`. For sub-files under `references/`, `scripts/`, or `assets/`,
  call `read_skill_file(name, relpath)` for the specific files required —
  do not bulk-load.
- Context hygiene: summarise rather than paste; only load what you need."""


_USAGE_RULES_EAGER = """\
### How to use skills
- The skills above are inlined in this prompt; refer to them by name.
- Trigger: if the user names a skill (with `$SkillName` or by plain match)
  or the task fits a skill's description, follow that skill's body.
- For sub-files under `references/` / `scripts/` / `assets/`, call
  `read_skill_file(name, relpath)` to pull a specific file."""


@dataclass
class Skill:
    """A discovered skill bundle."""

    name: str
    description: str
    content: str
    path: Path | None = None
    triggers: list[str] = field(default_factory=list)

    def read_file(self, relpath: str) -> str:
        """Return the contents of ``relpath`` resolved under this skill's dir.

        Raises :class:`ToolError` if ``self.path`` is unset (catalog built
        from in-memory ``Skill`` objects) or if ``relpath`` resolves outside
        the skill directory.
        """
        if self.path is None:
            raise ToolError(
                f"Skill {self.name!r} has no on-disk path; sub-files are unavailable.",
                tool_name="read_skill_file",
            )
        root = self.path.resolve()
        target = (root / relpath).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ToolError(
                f"Path {relpath!r} escapes skill directory.",
                hint="Use a relative path under references/, scripts/, or assets/.",
                tool_name="read_skill_file",
            ) from exc
        if not target.is_file():
            raise ToolError(
                f"Skill file not found: {relpath}", tool_name="read_skill_file"
            )
        return target.read_text(encoding="utf-8")


class SkillCatalog:
    """A collection of skills, resolved from a directory of ``SKILL.md`` files."""

    def __init__(
        self,
        skills: list[Skill],
        *,
        mode: SkillMode = "lazy",
        usage_rules: bool = True,
    ) -> None:
        self._skills = {s.name: s for s in skills}
        self.mode: SkillMode = mode
        self.usage_rules = usage_rules

    @classmethod
    def from_dir(
        cls,
        path: str | Path,
        *,
        mode: SkillMode = "lazy",
        usage_rules: bool = True,
    ) -> "SkillCatalog":
        """Scan ``path`` for ``*/SKILL.md`` and build a catalog."""
        root = Path(path)
        skills: list[Skill] = []
        if not root.exists():
            return cls(skills, mode=mode, usage_rules=usage_rules)
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "SKILL.md"
            if not manifest.exists():
                continue
            raw = manifest.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            triggers_raw = meta.get("triggers", "")
            triggers = [
                t.strip() for t in triggers_raw.strip("[]").split(",") if t.strip()
            ] if triggers_raw else []
            skills.append(
                Skill(
                    name=meta.get("name", entry.name),
                    description=meta.get(
                        "description", body.splitlines()[0] if body else ""
                    ),
                    content=raw,
                    path=entry,
                    triggers=triggers,
                )
            )
        return cls(skills, mode=mode, usage_rules=usage_rules)

    # ------------------------------------------------------------------ #
    def names(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def render_catalog(self) -> str:
        """Return a system-prompt fragment describing every skill.

        Format depends on ``mode``: lazy lists name + description + path
        (plus triggers when given); eager additionally inlines the body of
        each ``SKILL.md``. A short usage-rules paragraph is appended when
        ``usage_rules=True``.
        """
        if not self._skills:
            return ""
        lines: list[str]
        if self.mode == "eager":
            lines = ["## Skills (inlined)"]
            for s in self._skills.values():
                header = f"### {s.name} — {s.description}"
                lines.append(header)
                lines.append(s.content.strip())
        else:
            lines = ["## Skills (use load_skill to read in full)"]
            for s in self._skills.values():
                trail = f"  [path: {s.path}]" if s.path else ""
                trig = f"  triggers: {', '.join(s.triggers)}" if s.triggers else ""
                lines.append(f"- `${s.name}` — {s.description}{trail}{trig}")
        if self.usage_rules:
            rules = _USAGE_RULES_EAGER if self.mode == "eager" else _USAGE_RULES_LAZY
            lines.append("")
            lines.append(rules)
        return "\n".join(lines)

    def tools(self) -> list[Tool]:
        """Return the tools that expose this catalog to the model.

        ``list_skills`` is always present. In lazy mode ``load_skill`` and
        ``read_skill_file`` are added; in eager mode only
        ``read_skill_file`` joins (bodies are already in the prompt).
        """
        from .tools import tool as _tool

        async def list_skills() -> str:
            """List all available skills with descriptions."""
            return self.render_catalog() or "(no skills)"

        async def load_skill(name: str) -> str:
            """Load the full SKILL.md contents of a named skill."""
            skill = self.get(name.lstrip("$"))
            if skill is None:
                return (
                    f"Unknown skill: {name}. "
                    f"Available: {', '.join(self.names()) or '(none)'}"
                )
            return skill.content

        async def read_skill_file(name: str, path: str) -> str:
            """Read a sub-file under a skill (e.g. ``references/foo.md``)."""
            skill = self.get(name.lstrip("$"))
            if skill is None:
                return f"Unknown skill: {name}"
            try:
                return skill.read_file(path)
            except ToolError as exc:
                return str(exc)

        tools: list[Tool] = [_tool(list_skills), _tool(read_skill_file)]
        if self.mode == "lazy":
            tools.insert(1, _tool(load_skill))
        return tools


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
