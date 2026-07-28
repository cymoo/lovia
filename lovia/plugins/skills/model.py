"""Skill data model: errors, name/description validation, and the two
skill shapes — :class:`SkillMetadata` (the index entry) and :class:`Skill`
(the loaded skill)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ...types import JsonValue

# ---------------------------------------------------------------------------
# Errors
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


class SkillNotFoundError(SkillsError):
    """Raised when no skill answers to the requested name.

    ``available`` lists the names the caller may use instead; it is folded
    into the hint so the model can self-correct without a second lookup.
    """

    def __init__(self, name: str, *, available: Sequence[str] = ()) -> None:
        super().__init__(
            f"Unknown skill: {name!r}.",
            skill_name=name,
            hint=f"Available: {', '.join(available) or '(none)'}",
        )
        self.available = list(available)


# ---------------------------------------------------------------------------
# Name / description validation
# ---------------------------------------------------------------------------

# Matched with fullmatch(): `$` would accept a trailing newline (YAML quoted
# scalars can smuggle one in), fullmatch() does not.
_NAME_PATTERN = re.compile(r"[a-zA-Z0-9]+(?:[-_][a-zA-Z0-9]+)*")
_NAME_MAX_LENGTH = 64
_DESCRIPTION_MAX_LENGTH = 1024


def _validate_name(name: object) -> str:
    """Validate *name* (a string of letters, digits, hyphens and underscores, ≤ 64 chars)."""
    if name is None or name == "":
        raise SkillsError(
            "Skill name must not be empty.",
            hint="Provide a non-empty name, e.g. 'refund-policy'.",
        )
    # YAML happily yields ints/bools/dates; reject them explicitly instead of
    # crashing on len()/regex below.
    if not isinstance(name, str):
        raise SkillsError(
            f"Skill name must be a string, got {type(name).__name__}: {name!r}.",
            hint='Quote the frontmatter value, e.g. name: "2024".',
        )
    if len(name) > _NAME_MAX_LENGTH:
        raise SkillsError(
            f"Skill name {name!r} is too long ({len(name)} > {_NAME_MAX_LENGTH}).",
            skill_name=name,
            hint="Use a shorter name.",
        )
    # Security: reject path separators and traversal before format check
    if "/" in name or "\\" in name or ".." in name:
        raise SkillsError(
            f"Skill name {name!r} must not contain path separators or '..'.",
            skill_name=name,
            hint="Use a flat name without path characters.",
        )
    if not _NAME_PATTERN.fullmatch(name):
        raise SkillsError(
            f"Skill name {name!r} is invalid: "
            f"only letters, digits, hyphens, and underscores; no consecutive or "
            f"leading/trailing separators.",
            skill_name=name,
            hint="Rename to something like 'my-skill-name' or 'My_Skill'.",
        )
    return name


def _validate_description(name: str, description: object) -> str:
    """Validate *description* and return it stripped of surrounding whitespace."""
    if description is None or (
        isinstance(description, str) and not description.strip()
    ):
        raise SkillsError(
            f"Skill {name!r} has an empty description.",
            skill_name=name,
            hint="Provide a description explaining what the skill does and when to use it.",
        )
    if not isinstance(description, str):
        raise SkillsError(
            f"Skill {name!r} description must be a string, "
            f"got {type(description).__name__}.",
            skill_name=name,
            hint='Quote the frontmatter value, e.g. description: "...".',
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
    """Identifier: letters, digits, hyphens and underscores, max 64 characters."""

    description: str
    """What the skill does and when to use it, max 1024 characters."""

    extra: Mapping[str, JsonValue] = field(default_factory=dict)
    """Any frontmatter keys beyond ``name``/``description`` (tags, version, …),
    surfaced in the system-prompt index so the model can route on them."""


@dataclass
class Skill:
    """Level 2 — the full skill, loaded on demand.

    Created by a :class:`~lovia.plugins.SkillSource` when the model calls
    the ``load_skill`` tool.
    """

    name: str
    description: str
    body: str
    """``SKILL.md`` body text, without YAML frontmatter."""

    path: Path | None = None
    """On-disk directory, used by :meth:`read_file` to resolve sub-resources."""

    extra: Mapping[str, JsonValue] = field(default_factory=dict)
    """Extra frontmatter keys carried over from :class:`SkillMetadata`."""

    # -- sub-resource access ------------------------------------------------ #

    def read_file(self, relpath: str) -> str:
        """Return the contents of *relpath* resolved under this skill's directory.

        Raises :class:`SkillsError` when *self.path* is unset (e.g. in-memory
        skills), *relpath* escapes the skill directory, or the target file does
        not exist or cannot be read as UTF-8 text. The tool layer is responsible
        for turning this into a model-facing message — the data model stays free
        of tool concerns.
        """
        if self.path is None:
            raise SkillsError(
                f"Skill {self.name!r} has no on-disk path; sub-files are unavailable.",
                skill_name=self.name,
                path=relpath,
            )
        root = self.path.resolve()
        target = (root / relpath).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise SkillsError(
                f"Path {relpath!r} escapes skill directory.",
                skill_name=self.name,
                path=relpath,
                hint="Use a relative path inside the skill directory.",
            ) from None
        if not target.is_file():
            raise SkillsError(
                f"Skill file not found: {relpath}",
                skill_name=self.name,
                path=relpath,
            )
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SkillsError(
                f"Skill file {relpath!r} is not UTF-8 text (a binary asset?).",
                skill_name=self.name,
                path=relpath,
                hint="Only text files can be read into context; reference "
                "binary assets by path instead.",
            ) from None
        except OSError as exc:
            raise SkillsError(
                f"Failed to read skill file {relpath!r}: {exc}",
                skill_name=self.name,
                path=relpath,
            ) from exc

    # -- derived ------------------------------------------------------------ #

    @property
    def metadata(self) -> SkillMetadata:
        """Derive the Level-1 index entry from this skill."""
        return SkillMetadata(
            name=self.name, description=self.description, extra=self.extra
        )
