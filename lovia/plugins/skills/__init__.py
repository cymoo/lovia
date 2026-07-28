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

* **Level 1 (metadata)** — ``name`` + ``description`` (plus any extra
  frontmatter keys) always injected into the system prompt so the model knows
  what's available.
* **Level 2 (instructions)** — the full ``SKILL.md`` body, loaded on demand via
  the ``load_skill`` tool. Bodies are read lazily and never held in memory.
* **Level 3 (resources)** — sub-files under the skill directory, read via
  ``read_skill_file``. The body names the files it needs, so the model never
  has to guess paths.

:class:`SkillCatalog` combines a :class:`SkillSource` with usage rules and a
visibility filter, and renders ``instructions()`` and ``tools()``. Wrap it
with the :class:`Skills` plugin to attach it to an :class:`~lovia.Agent`, or
use it standalone for programmatic access.

Extension: implement the :class:`SkillSource` protocol — two async methods,
``list_skills()`` and ``load_skill(name)`` — to serve skills from databases,
APIs, MCP servers, or any other backend. Sources are expected to return
*current* truth on every call (the built-in :class:`DirectorySkillSource`
re-scans its directories), so skills added at runtime are picked up without
restarts or reload plumbing.

Untrusted content: skill files are author-supplied, so both tools frame what
they return between BEGIN/END SKILL CONTENT markers and their descriptions
tell the model to treat it as reference material — instructions inside a
skill cannot override the system prompt.

Workspace interplay: ``load_skill`` / ``read_skill_file`` do their own file IO
— a workspace ACL does not gate them, so skills load even when their directory
lies outside the workspace root (or matches ``denied_paths``). Treat a skill
directory as trusted, fully exposed content. Only *executing* bundled scripts
goes through the workspace shell and its policy: when skills bundle scripts,
grant the skill directory via ``Workspace.local(readable=...)`` so running
them doesn't trip outside-root approval.
"""

from .catalog import SkillCatalog, SkillFilter
from .model import Skill, SkillMetadata, SkillNotFoundError, SkillsError
from .plugin import Skills
from .source import DirectorySkillSource, SkillSource

__all__ = [
    "DirectorySkillSource",
    "Skill",
    "SkillCatalog",
    "SkillFilter",
    "SkillMetadata",
    "SkillNotFoundError",
    "SkillSource",
    "Skills",
    "SkillsError",
]
