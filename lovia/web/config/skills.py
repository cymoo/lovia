"""Skill-directory resolution and discovery for the web UI.

``SkillsConfig.dirs`` is the single source of truth for where skills load
from; this module turns it into filesystem roots (:func:`resolve_skills_dirs`,
used by the agent builder) and into a per-directory discovery report
(:func:`scan_skills_status`, served by ``GET /api/config/skills`` so the
Settings pane can show what each directory contributes).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ...plugins.skills import DirectorySkillSource, SkillsError
from .schema import DEFAULT_SKILLS_DIR, USER_SKILLS_DIR, SkillsConfig

log = logging.getLogger("lovia.web.config")


@dataclass(frozen=True)
class SkillRoot:
    """One configured skill directory, expanded but not required to exist."""

    kind: str  # "project_default" | "user_default" | "custom"
    path: str  # as configured (display form, e.g. "~/.agents/skills")
    resolved: Path  # expanduser'd
    exists: bool


def _kind(raw: str) -> str:
    if raw == DEFAULT_SKILLS_DIR:
        return "project_default"
    if raw == USER_SKILLS_DIR:
        return "user_default"
    return "custom"


def skill_roots(skills: SkillsConfig) -> list[SkillRoot]:
    """Every configured root in precedence order, deduplicated by identity."""
    roots: list[SkillRoot] = []
    seen: set[Path] = set()
    for raw in skills.dirs:
        expanded = Path(raw).expanduser()
        key = expanded.resolve()
        if key in seen:
            continue
        seen.add(key)
        roots.append(
            SkillRoot(
                kind=_kind(raw), path=raw, resolved=expanded, exists=expanded.is_dir()
            )
        )
    return roots


def resolve_skills_dirs(skills: SkillsConfig | None) -> list[Path]:
    """The existing skill roots, in precedence order (first root wins).

    Never raises: a listed directory that doesn't exist is skipped with an
    info log (the defaults are commonly absent; the Settings pane reports
    per-directory status), so a stale ``config.json`` can never make the
    server unbootable. A bare ``./skills`` was the default before 0.9.13;
    it is no longer auto-loaded (the generic name collides with non-skill
    directories), so we point at the move once at startup.
    """
    skills = skills or SkillsConfig()
    dirs: list[Path] = []
    for root in skill_roots(skills):
        if not root.exists:
            if root.kind == "project_default" and Path("skills").is_dir():
                log.warning(
                    "./skills is no longer auto-loaded; move it to ./%s "
                    "or add it in Settings -> Skills",
                    DEFAULT_SKILLS_DIR,
                )
            else:
                log.info("skills directory not found, skipped: %s", root.path)
            continue
        dirs.append(root.resolved)
    return dirs


@dataclass
class SkillEntry:
    name: str
    description: str
    shadowed: bool = False  # an earlier existing root already claims this name


@dataclass
class SkillProblem:
    dir: str  # subdirectory name inside the root
    error: str  # human-readable parse/validation failure


@dataclass
class RootStatus:
    """What one configured directory contributes, for the Settings pane."""

    kind: str
    path: str
    resolved: str
    exists: bool
    skills: list[SkillEntry] = field(default_factory=list)
    problems: list[SkillProblem] = field(default_factory=list)


def scan_skills_status(skills: SkillsConfig) -> list[RootStatus]:
    """Discover skills per configured root (sync; call via ``asyncio.to_thread``).

    Reuses ``DirectorySkillSource._read_metadata`` on a throwaway instance so
    frontmatter parsing and name/description validation can never drift from
    what the served plugin accepts (private-member reuse within the project,
    in lieu of duplicating the logic here).
    """
    statuses: list[RootStatus] = []
    claimed: set[str] = set()
    for root in skill_roots(skills):
        status = RootStatus(
            kind=root.kind,
            path=root.path,
            resolved=str(root.resolved),
            exists=root.exists,
        )
        statuses.append(status)
        if not root.exists:
            continue
        source = DirectorySkillSource(root.resolved)
        try:
            entries = sorted(root.resolved.iterdir())
        except OSError as exc:
            status.problems.append(SkillProblem(dir=".", error=str(exc)))
            continue
        for entry in entries:
            try:
                meta = source._read_metadata(entry)
            except (OSError, UnicodeDecodeError, SkillsError) as exc:
                status.problems.append(SkillProblem(dir=entry.name, error=str(exc)))
                continue
            if meta is None:
                continue
            status.skills.append(
                SkillEntry(
                    name=meta.name,
                    description=meta.description,
                    shadowed=meta.name in claimed,
                )
            )
        # Claim after the whole root: duplicates *within* a root are a
        # DirectorySkillSource concern; across roots the earlier root wins.
        claimed.update(s.name for s in status.skills if not s.shadowed)
    return statuses
