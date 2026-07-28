"""Skill storage: the :class:`SkillSource` protocol and the built-in
directory-backed source."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml  # type: ignore[import-untyped]

from .model import (
    Skill,
    SkillMetadata,
    SkillNotFoundError,
    SkillsError,
    _validate_description,
    _validate_name,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skill source protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SkillSource(Protocol):
    """Where skills come from — a directory, database, API, MCP server, …

    Two async methods, each expected to return *current* truth:

    * :meth:`list_skills` — the Level-1 index entries. Called when the
      system-prompt index is rendered (once per run) and on demand, so a
      live backend simply queries per call; a caching backend owns its own
      invalidation.
    * :meth:`load_skill` — the full :class:`Skill`, called when the model
      invokes the ``load_skill`` tool. Raises :class:`SkillNotFoundError`
      for unknown names.

    There is deliberately no reload/refresh member: a source that can go
    stale re-checks its backend inside these two calls instead (see
    :class:`DirectorySkillSource` for the pattern), so dynamically added
    skills need no out-of-band invalidation step.
    """

    async def list_skills(self) -> list[SkillMetadata]:
        """Return index entries for every available skill (Level 1)."""
        ...

    async def load_skill(self, name: str) -> Skill:
        """Return the full :class:`Skill` for *name* (Level 2).

        Raises :class:`SkillNotFoundError` when *name* is unknown.
        """
        ...


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

# Both delimiters must be a "---" on its own line (trailing whitespace
# tolerated). For the opening this keeps a frontmatter-less body that starts
# with a Markdown thematic break (`----`) or stray text (`---junk`) intact
# instead of being mis-parsed as frontmatter. For the closing, column 0 is
# the only robust boundary: YAML block scalars are always indented, so a
# column-0 "---" can never occur inside one.
_OPENING_FM = re.compile(r"---[ \t\r]*\n")
_CLOSING_FM = re.compile(r"\n---[ \t\r]*(?:\n|$)")


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from the leading ``---`` block.

    Returns ``(metadata_dict, body_text)``. When no frontmatter is present
    returns ``({}, text)``.

    Tolerates leading blank lines and trailing whitespace on the ``---``
    delimiters. If there is no closing delimiter the file has no valid
    frontmatter, so it is treated entirely as body — guessing with a naive
    substring search would truncate any value that contains "---". Malformed
    YAML (or YAML that is not a mapping) yields ``{}`` so the caller's
    name/description validation produces the actual error.
    """

    trimmed = text.lstrip()
    opening = _OPENING_FM.match(trimmed)
    if not opening:
        return {}, text

    # Keep the newline that ends the opening line in the search space so the
    # closing pattern (`\n---…`) can match a zero-line frontmatter.
    rest = trimmed[opening.end() - 1 :]
    closing = _CLOSING_FM.search(rest)
    if not closing:
        return {}, text

    fm_text = rest[1 : closing.start()].strip()
    body = rest[closing.end() :].lstrip("\n\r")

    if not fm_text:
        return {}, body

    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, body
    if isinstance(parsed, dict):
        return parsed, body
    return {}, body


# ---------------------------------------------------------------------------
# Built-in source: local directories
# ---------------------------------------------------------------------------


class DirectorySkillSource:
    """Serve skills from ``<root>/<skill-name>/SKILL.md`` directories.

    The source is *live*: every :meth:`list_skills` re-scans the roots (on a
    worker thread), and a :meth:`load_skill` miss triggers one re-scan before
    failing — so skills added or removed at runtime are picked up without
    restarts or explicit invalidation. A scan reads one ``SKILL.md`` per
    skill and keeps only lightweight metadata; bodies are read on each load
    and never cached, so edits on disk take effect immediately and memory
    stays flat regardless of catalog size.

    When the same skill name appears in more than one place, the first
    occurrence (in root order, then directory-sort order) wins; later ones
    are skipped. Scan problems — unreadable files, invalid frontmatter,
    duplicates, missing roots — are logged once per process, not on every
    scan.
    """

    def __init__(self, *roots: str | Path) -> None:
        # Roots are ~-expanded and resolved to absolute paths so Skill.path
        # (and the `path:` hint in load_skill output) stays unambiguous: the
        # workspace tools resolve relative paths against the workspace root,
        # not this process's cwd.
        self._roots = [Path(r).expanduser().resolve() for r in roots]
        # Lookup table from the most recent scan. One dict so metadata and
        # directory can never skew; replaced wholesale (never mutated) so
        # readers on other threads see a consistent snapshot.
        self._by_name: dict[str, tuple[SkillMetadata, Path]] = {}
        self._warned: set[str] = set()

    # -- SkillSource -------------------------------------------------------- #

    async def list_skills(self) -> list[SkillMetadata]:
        index = await asyncio.to_thread(self._scan)
        return [meta for meta, _ in index.values()]

    async def load_skill(self, name: str) -> Skill:
        # File IO runs on a worker thread so a slow disk never blocks the loop.
        return await asyncio.to_thread(self._load, name)

    # -- internal ----------------------------------------------------------- #

    def _load(self, name: str) -> Skill:
        record = self._by_name.get(name)
        if record is None:
            # Self-heal: the skill may have appeared since the last scan
            # (e.g. the agent just wrote it). One extra scan on the miss
            # path is cheap and keeps "add skill → load it" restart-free.
            record = self._scan().get(name)
        if record is None:
            raise SkillNotFoundError(name, available=sorted(self._by_name))
        meta, skill_dir = record
        return Skill(
            name=meta.name,
            description=meta.description,
            body=self._read_body(name, skill_dir),
            path=skill_dir,
            extra=meta.extra,
        )

    def _scan(self) -> dict[str, tuple[SkillMetadata, Path]]:
        """Scan every root, returning (and atomically publishing) a fresh index.

        Each entry is isolated — one broken or invalid skill directory does
        not block the rest.
        """
        index: dict[str, tuple[SkillMetadata, Path]] = {}
        for root in self._roots:
            if not root.is_dir():
                self._warn_once(f"root:{root}", "skill.root_missing: %s", root)
                continue
            try:
                entries = sorted(root.iterdir())
            except OSError as exc:
                self._warn_once(
                    f"root:{root}:{exc}", "skill.root_unreadable: %s (%s)", root, exc
                )
                continue

            for entry in entries:
                try:
                    meta = self._read_metadata(entry)
                except (OSError, UnicodeDecodeError) as exc:
                    self._warn_once(
                        f"entry:{entry}:{exc}", "skill.unreadable: %s (%s)", entry, exc
                    )
                    continue
                except SkillsError as exc:
                    self._warn_once(
                        f"entry:{entry}:{exc}", "skill.invalid: %s (%s)", entry, exc
                    )
                    continue
                if meta is None:
                    continue
                if meta.name in index:
                    self._warn_once(
                        f"dup:{meta.name}:{entry}",
                        "skill.duplicate: %r in %s, skipped",
                        meta.name,
                        entry,
                    )
                    continue
                index[meta.name] = (meta, entry)
        self._by_name = index
        return index

    def _read_metadata(self, entry: Path) -> SkillMetadata | None:
        """Parse and validate *entry*'s frontmatter; None if it is not a skill dir."""
        if not entry.is_dir():
            return None
        manifest = entry / "SKILL.md"
        if not manifest.is_file():
            return None
        frontmatter, _ = _parse_frontmatter(manifest.read_text(encoding="utf-8"))
        name = _validate_name(frontmatter.get("name", entry.name))
        description = _validate_description(name, frontmatter.get("description", ""))
        extra = {
            k: v for k, v in frontmatter.items() if k not in ("name", "description")
        }
        return SkillMetadata(name=name, description=description, extra=extra)

    def _read_body(self, name: str, skill_dir: Path) -> str:
        """Read the ``SKILL.md`` body for *name* from *skill_dir*, stripping frontmatter."""
        manifest = skill_dir / "SKILL.md"
        try:
            raw = manifest.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise SkillsError(
                f"Skill {name!r} SKILL.md is not UTF-8 text.",
                skill_name=name,
                path=str(skill_dir),
                hint="Re-save the file with UTF-8 encoding.",
            ) from None
        except OSError as exc:
            raise SkillsError(
                f"Failed to read skill {name!r}: {exc}",
                skill_name=name,
                path=str(skill_dir),
                hint="Check file permissions.",
            ) from exc
        _, body = _parse_frontmatter(raw)
        return body

    def _warn_once(self, key: str, msg: str, *args: object) -> None:
        """Log *msg* at WARNING the first time *key* is seen.

        The live source re-scans on every run; without deduplication a
        broken skill directory would spam the log once per run.
        """
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(msg, *args)
