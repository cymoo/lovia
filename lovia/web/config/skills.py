"""Skill-directory resolution and discovery for the web UI.

Skills load from two places: the project's own ``.agents/skills`` (a fixed
convention, in the root of the chat's workspace) and ``SkillsConfig.dirs``,
the global directories. This module turns both into filesystem roots
(:func:`resolve_skills_dirs`, used by the agent builder), and the global
ones into a per-directory discovery report (:func:`scan_skills_status`,
served by ``GET /api/config/skills`` so the Settings pane can show what each
directory contributes).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ...plugins.skills import DirectorySkillSource, SkillsError
from .schema import PROJECT_SKILLS_DIR, USER_SKILLS_DIR, SkillsConfig

log = logging.getLogger("lovia.web.config")


@dataclass(frozen=True)
class SkillRoot:
    """One configured skill directory, expanded but not required to exist."""

    kind: str  # "default" | "custom"
    path: str  # as configured (display form, e.g. "~/.agents/skills")
    resolved: Path  # expanduser'd
    exists: bool


def skill_roots(skills: SkillsConfig) -> list[SkillRoot]:
    """Every configured entry in precedence order — one row per string.

    Deliberately no identity folding: entries that resolve to the same
    directory each keep their own row, so the Settings pane can remove any
    of them; :func:`resolve_skills_dirs` dedupes when building the actual
    plugin roots.
    """
    roots: list[SkillRoot] = []
    for raw in skills.dirs:
        expanded = Path(raw).expanduser()
        roots.append(
            SkillRoot(
                kind="default" if raw == USER_SKILLS_DIR else "custom",
                path=raw,
                resolved=expanded,
                exists=expanded.is_dir(),
            )
        )
    return roots


def resolve_skills_dirs(
    skills: SkillsConfig | None,
    project: Path | None = None,
    *,
    quiet: bool = False,
) -> list[Path]:
    """The existing skill roots, in precedence order (first root wins): the
    ``project``'s own ``.agents/skills``, then the global directories.

    Never raises: a listed directory that doesn't exist is skipped with an
    info log (the default is commonly absent; the Settings pane reports
    per-directory status), so a stale ``config.json`` can never make the
    server unbootable. ``quiet`` skips the logging, for a per-run call.
    """
    skills = skills or SkillsConfig()
    candidates: list[Path] = []
    if project is not None and (project / PROJECT_SKILLS_DIR).is_dir():
        candidates.append(project / PROJECT_SKILLS_DIR)
    for root in skill_roots(skills):
        if root.exists:
            candidates.append(root.resolved)
        elif not quiet:
            log.info("skills directory not found, skipped: %s", root.path)
    dirs: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            dirs.append(path)
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
    """Discover skills per global root (sync; call via ``asyncio.to_thread``).

    A project's own skills are not listed: they belong to wherever a chat
    works, not to the server. Reuses ``DirectorySkillSource._read_metadata``
    on a throwaway instance so frontmatter parsing and name/description
    validation can never drift from what the served plugin accepts
    (private-member reuse within the project, in lieu of duplicating the
    logic here).
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
