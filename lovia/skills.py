"""Skill system: progressive disclosure of reusable instruction bundles.

A *skill* is a directory containing a ``SKILL.md`` file with YAML frontmatter
following the `Agent Skills specification <https://agentskills.io/specification>`_::

    ---
    name: refund-policy
    description: Process customer refunds and handle return requests.
    ---
    # Refund Policy
    ...

Optional subdirectories for supplementary resources:

* ``references/`` — detailed docs the model loads on demand.
* ``scripts/``    — executable snippets.
* ``assets/``     — templates, fixtures, etc.

Architecture
------------

Three layers of progressive disclosure:

* **Level 1 (metadata)** — ``name`` + ``description`` always injected into the
  system prompt so the model knows what's available.
* **Level 2 (instructions)** — the full ``SKILL.md`` body loaded on demand via
  the ``load_skill`` tool.
* **Level 3 (resources)** — sub-files under the skill directory read via
  ``read_skill_file``.

:class:`Skills` mirrors the :class:`~lovia.sandbox.Sandbox` pattern — both
expose ``instructions()`` (system prompt fragment) and ``tools()`` (model-facing
tools), making them peer capabilities on an :class:`~lovia.Agent`.

Extension: implement the :class:`SkillSource` protocol to serve skills from
databases, APIs, MCP servers, or any other backend.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .exceptions import ToolError
from .tools import Tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SkillsError(Exception):
    """Raised when a skill fails to load or validate.

    Carries structured context for programmatic handling and clear error
    messages for humans and models alike.  The *hint* is folded into the
    string representation so the model can act on it.
    """

    def __init__(
        self,
        message: str,
        *,
        skill_name: str | None = None,
        path: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.skill_name = skill_name
        self.path = path
        self.hint = hint

    def __str__(self) -> str:
        msg = self.args[0] if self.args else ""
        if self.hint:
            msg = f"{msg}  {self.hint}"
        return msg


# ---------------------------------------------------------------------------
# Name / description validation
# ---------------------------------------------------------------------------

_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NAME_MAX_LENGTH = 64
_DESCRIPTION_MAX_LENGTH = 1024
_CLOSING_FM = re.compile(r"\n---[ \t\r]*(?:\n|$)")


def _validate_name(name: str) -> str:
    """Validate *name* follows the Agent Skills spec (kebab-case, ≤ 64 chars)."""
    if not name:
        raise SkillsError(
            "Skill name must not be empty.",
            skill_name=name,
            hint="Provide a non-empty kebab-case name, e.g. 'refund-policy'.",
        )
    if len(name) > _NAME_MAX_LENGTH:
        raise SkillsError(
            f"Skill name {name!r} is too long ({len(name)} > {_NAME_MAX_LENGTH}).",
            skill_name=name,
            hint="Use a shorter kebab-case name.",
        )
    # Security: reject path separators and traversal before format check
    if "/" in name or "\\" in name or ".." in name:
        raise SkillsError(
            f"Skill name {name!r} must not contain path separators or '..'.",
            skill_name=name,
            hint="Use a flat kebab-case name.",
        )
    if not _NAME_PATTERN.match(name):
        raise SkillsError(
            f"Skill name {name!r} must be kebab-case: "
            f"lowercase letters, digits, and single hyphens only.",
            skill_name=name,
            hint="Rename to something like 'my-skill-name'.",
        )
    return name


def _validate_description(name: str, description: str) -> str:
    """Validate *description* is non-empty and within length limits."""
    if not description or not description.strip():
        raise SkillsError(
            f"Skill {name!r} has an empty description.",
            skill_name=name,
            hint="Provide a description explaining what the skill does and when to use it.",
        )
    if len(description) > _DESCRIPTION_MAX_LENGTH:
        raise SkillsError(
            f"Skill {name!r} description is too long "
            f"({len(description)} > {_DESCRIPTION_MAX_LENGTH}).",
            skill_name=name,
            hint="Shorten the description to at most 1024 characters.",
        )
    return description.strip()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillMetadata:
    """Level 1 — lightweight index entry always visible in the system prompt.

    Contains just enough information for the model to decide whether to
    ``load_skill``.
    """

    name: str
    """Kebab-case identifier, max 64 characters."""

    description: str
    """What the skill does and when to use it, max 1024 characters."""


@dataclass
class Skill:
    """Level 2 — the full skill, loaded on demand.

    Created by a :class:`SkillSource` when the model calls ``load_skill``.
    """

    name: str
    description: str
    content: str
    """``SKILL.md`` body text, without YAML frontmatter."""

    path: Path | None = None
    """On-disk directory, used by :meth:`read_file` to resolve sub-resources."""

    # -- sub-resource access ------------------------------------------------ #

    def read_file(self, relpath: str) -> str:
        """Return the contents of *relpath* resolved under this skill's directory.

        Raises :class:`~lovia.exceptions.ToolError` when *self.path* is unset
        (e.g. in-memory skills), *relpath* escapes the skill directory, or the
        target file does not exist.
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
        except ValueError:
            raise ToolError(
                f"Path {relpath!r} escapes skill directory.",
                hint="Use a relative path inside the skill directory.",
                tool_name="read_skill_file",
            ) from None
        if not target.is_file():
            raise ToolError(
                f"Skill file not found: {relpath}", tool_name="read_skill_file"
            )
        return target.read_text(encoding="utf-8")

    # -- derived ------------------------------------------------------------ #

    @property
    def metadata(self) -> SkillMetadata:
        """Derive the Level-1 index entry from this skill."""
        return SkillMetadata(name=self.name, description=self.description)


# ---------------------------------------------------------------------------
# Skill source protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SkillSource(Protocol):
    """Abstract source of skills — filesystem, database, API, MCP, etc.

    Two members:

    * ``metadata`` — sync property returning lightweight index entries.
    * ``load(name)`` — returns the full :class:`Skill`. Called lazily when
      the model invokes ``load_skill``.
    """

    metadata: list[SkillMetadata]
    """Lightweight index entries for every available skill (Level 1)."""

    async def load(self, name: str) -> Skill:
        """Return the full :class:`Skill` for *name* (Level 2).

        Raises :class:`SkillsError` when *name* is unknown.
        """
        ...


# ---------------------------------------------------------------------------
# Built-in source: local directory
# ---------------------------------------------------------------------------


class LocalDirSkillSource:
    """Scan a local directory for ``*/SKILL.md`` files.

    Metadata and body text are eagerly cached at construction time so
    :meth:`~Skills.instructions` is synchronous and :meth:`load` never
    re-reads a file (unless :meth:`invalidate` is called first).
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._metadata: dict[str, SkillMetadata] = {}
        self._bodies: dict[str, str] = {}  # name → SKILL.md body
        self._skill_dirs: dict[str, Path] = {}
        self._scan()

    # -- metadata ----------------------------------------------------------- #

    @property
    def metadata(self) -> list[SkillMetadata]:
        return list(self._metadata.values())

    # -- load --------------------------------------------------------------- #

    async def load(self, name: str) -> Skill:
        if name not in self._metadata:
            known = ", ".join(sorted(self._metadata.keys())) or "(none)"
            raise SkillsError(
                f"Unknown skill: {name!r}.",
                skill_name=name,
                hint=f"Available: {known}",
            )
        meta = self._metadata[name]
        body = self._bodies.get(name)
        if body is None:
            body = self._read_body(name)
            self._bodies[name] = body
        return Skill(
            name=meta.name,
            description=meta.description,
            content=body,
            path=self._skill_dirs[name],
        )

    # -- cache invalidation ------------------------------------------------- #

    def invalidate(self, name: str) -> None:
        """Drop cached body so the next :meth:`load` re-reads from disk."""
        self._bodies.pop(name, None)

    def invalidate_all(self) -> None:
        """Drop all cached bodies."""
        self._bodies.clear()

    # -- internal ----------------------------------------------------------- #

    def _scan(self) -> None:
        """Eagerly scan the root directory, caching metadata and bodies.

        Each entry is isolated — one broken or invalid skill directory
        does not block the rest.
        """
        if not self._root.exists():
            return
        try:
            entries = sorted(self._root.iterdir())
        except OSError:
            return

        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
                manifest = entry / "SKILL.md"
                if not manifest.is_file():
                    continue
                raw = manifest.read_text(encoding="utf-8")
                frontmatter, body = _parse_frontmatter(raw)
                name = frontmatter.get("name", entry.name)
                description = frontmatter.get("description", "")
                _validate_name(name)
                _validate_description(name, description)
                if name in self._metadata:
                    logger.warning(
                        f"Duplicate skill name {name!r} in {entry}, skipped."
                    )
                    continue
                self._metadata[name] = SkillMetadata(name=name, description=description)
                self._bodies[name] = body
                self._skill_dirs[name] = entry
            except OSError as exc:
                logger.warning(f"Skipping unreadable skill directory {entry}: {exc}")
            except SkillsError as exc:
                logger.warning(f"Skipping invalid skill in {entry}: {exc}")

    def _read_body(self, name: str) -> str:
        """Return the body of a SKILL.md file, stripping the YAML frontmatter.

        Uses a simple regex cut rather than a full parse — the file was
        already validated during :meth:`_scan`, so we know the frontmatter
        is well-formed.
        """
        manifest = self._skill_dirs[name] / "SKILL.md"
        try:
            raw = manifest.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillsError(
                f"Failed to read skill {name!r}: {exc}",
                skill_name=name,
                path=str(self._skill_dirs[name]),
                hint="Check file permissions.",
            ) from exc
        raw = raw.lstrip()
        m = _CLOSING_FM.search(raw[3:])
        if m:
            return raw[3 + m.end() :].lstrip("\n\r")
        # Fallback: bare --- anywhere after the opening delimiter.
        parts = raw.split("---", 2)
        return parts[2].lstrip("\n\r") if len(parts) >= 3 else raw


# ---------------------------------------------------------------------------
# System prompt fragment
# ---------------------------------------------------------------------------

_DEFAULT_USAGE_RULES = """\
## Using skills
Skills provide domain-specific instructions, procedures, and reference material.
Each skill listed above has a description — use it to decide which are relevant.
- Call `load_skill(name)` to load a skill's full instructions.
- Call `read_skill_file(name, relpath)` to read supplementary files
  (e.g. `references/…`, `scripts/…`, `assets/…`).
- Load skills only when needed. Each one consumes context — prefer
  targeted loading over loading everything upfront."""


# ---------------------------------------------------------------------------
# Skills — capability container
# ---------------------------------------------------------------------------


class Skills:
    """A collection of skills exposed to the model as a capability.

    Mirrors the :class:`~lovia.sandbox.Sandbox` pattern: both provide
    ``instructions()`` (system prompt fragment) and ``tools()`` (model-facing
    tools). Attach to an :class:`~lovia.Agent` via the ``skills`` field.

    Usage::

        agent = Agent(
            name="bot",
            instructions="Be helpful.",
            skills=Skills.from_dir("./skills"),
        )
    """

    def __init__(
        self,
        source: SkillSource,
        *,
        usage_rules: str | None = None,
    ) -> None:
        self._source = source
        self._usage_rules = usage_rules  # None → default, "" → none, str → custom
        self._metadata = source.metadata  # protocol guarantees sync access

    # -- factories ---------------------------------------------------------- #

    @classmethod
    def from_dir(
        cls,
        path: str | Path,
        *,
        usage_rules: str | None = None,
    ) -> "Skills":
        """Scan *path* for ``*/SKILL.md`` and build a :class:`Skills` instance."""
        return cls(LocalDirSkillSource(path), usage_rules=usage_rules)

    # -- Capability interface ----------------------------------------------- #

    def instructions(self) -> str:
        """Render the skill index as a system-prompt fragment (Level 1).

        Synchronous because metadata is cached eagerly during ``__init__``.
        """
        if not self._metadata:
            return ""

        lines = ["## Skills"]
        for m in self._metadata:
            lines.append(f"- `{m.name}` — {m.description}")

        rules = self._usage_rules
        if rules is None:
            rules = _DEFAULT_USAGE_RULES
        if rules:
            lines.append("")
            lines.append(rules)

        return "\n".join(lines)

    def tools(self) -> list[Tool]:
        """Return the tools that expose this catalog to the model.

        Always includes ``list_skills`` and ``read_skill_file``, plus
        ``load_skill``.
        """
        from .tools import tool as _tool

        source = self._source

        @_tool
        async def list_skills() -> str:
            """List all available skills with their descriptions."""
            return self.instructions() or "(no skills available)"

        @_tool
        async def load_skill(name: str) -> str:
            """Load the full SKILL.md content of a named skill.

            Args:
                name: The skill name (with or without leading ``$``).
            """
            clean = name.lstrip("$")
            try:
                skill = await source.load(clean)
            except SkillsError as exc:
                return str(exc)
            header = f"[Skill: {skill.name}]\n"
            if skill.path is not None:
                header = f"[Skill: {skill.name}  path: {skill.path}]\n"
            return header + "\n" + skill.content

        @_tool
        async def read_skill_file(name: str, relpath: str) -> str:
            """Read a sub-file from a skill directory.

            Use for supplementary files like ``references/foo.md`` or
            ``scripts/run.py``.

            Args:
                name: The skill name.
                relpath: Relative path inside the skill directory.
            """
            clean = name.lstrip("$")
            try:
                skill = await source.load(clean)
            except SkillsError as exc:
                return str(exc)
            try:
                return skill.read_file(relpath)
            except ToolError as exc:
                return str(exc)

        return [
            list_skills,
            load_skill,
            read_skill_file,
        ]


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from the leading ``---`` block.

    Returns ``(metadata_dict, body_text)``. When no frontmatter is present
    returns ``({}, text)``.

    Tolerates leading blank lines and trailing whitespace on the ``---``
    delimiters.

    Uses ``yaml.safe_load`` (PyYAML is a transitive dependency). Falls back
    to a minimal line parser only when PyYAML is somehow unavailable — this
    path handles the simple ``key: value`` pairs used in ``SKILL.md`` metadata
    but does not aim to be a general-purpose YAML parser.
    """

    trimmed = text.lstrip()
    if not trimmed.startswith("---"):
        return {}, text

    # Find the closing "---" on its own line (possibly with trailing
    # whitespace and Windows-style line endings).
    m = _CLOSING_FM.search(trimmed[3:])
    if m:
        body_start = 3 + m.end()
        fm_text = trimmed[3 : 3 + m.start()].strip()
    else:
        end = trimmed.find("---", 3)
        if end == -1:
            return {}, text
        body_start = end + 3
        fm_text = trimmed[3:end].strip()

    body = trimmed[body_start:].lstrip("\n\r")

    if not fm_text:
        return {}, body

    # Primary path: PyYAML (available via jsonschema / pydantic dependency)
    try:
        import yaml  # type: ignore[import-untyped]

        parsed = yaml.safe_load(fm_text)
        if isinstance(parsed, dict):
            return parsed, body
    except Exception:
        pass

    # Fallback: minimal key:value parser for environments without PyYAML.
    # Handles quoted values, comments, and simple inline lists.
    meta: dict[str, Any] = {}
    for line in fm_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        k, raw = stripped.split(":", 1)
        k = k.strip()
        raw = raw.strip()
        # Unquote simple quoted strings
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
            raw = raw[1:-1]
        # Inline list: [a, b, c]
        if raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1]
            items = [it.strip().strip("\"'") for it in inner.split(",") if it.strip()]
            meta[k] = items
        else:
            meta[k] = raw

    return meta, body
