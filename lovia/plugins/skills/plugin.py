"""The :class:`Skills` plugin: attach a skill catalog to an agent."""

from __future__ import annotations

from pathlib import Path

from ...exceptions import UserError
from ..base import PluginInstance
from .catalog import SkillCatalog, SkillFilter
from .source import SkillSource


def _build_catalog(
    *sources: str | Path | SkillSource | SkillCatalog,
    usage_rules: str | None = None,
    filter: SkillFilter | None = None,
) -> SkillCatalog:
    """Resolve one or more sources into a :class:`SkillCatalog`."""
    if not sources:
        raise UserError(
            "Skills() needs at least one skill directory or source.",
            hint='e.g. Skills("./skills") or Skills(MySkillSource()).',
        )
    first = sources[0]
    if len(sources) == 1 and isinstance(first, SkillCatalog):
        if usage_rules is not None or filter is not None:
            raise UserError(
                "Configure usage_rules=/filter= on the SkillCatalog you build, "
                "not on Skills() — they would be ignored when a SkillCatalog is passed.",
            )
        return first
    if len(sources) == 1 and isinstance(first, SkillSource):
        return SkillCatalog(first, usage_rules=usage_rules, filter=filter)
    paths = [s for s in sources if isinstance(s, (str, Path))]
    if len(paths) != len(sources):
        raise UserError(
            "Skills() takes skill directories, or a single SkillSource / "
            "SkillCatalog — not a mix of the two.",
        )
    return SkillCatalog.from_dir(*paths, usage_rules=usage_rules, filter=filter)


class Skills:
    """Expose skills to an agent as a plugin.

    The common case is one or more directories, each holding ``<name>/SKILL.md``
    folders — pass the paths straight in::

        agent = Agent(..., plugins=[Skills("./skills")])
        agent = Agent(..., plugins=[Skills("./skills", "./team-skills")])

    Scope or relabel the catalog with keyword options (forwarded to
    :meth:`SkillCatalog.from_dir`)::

        plugins=[Skills("./skills", filter=lambda m: "beta" not in m.extra.get("tags", []))]

    For a custom backend, pass a :class:`SkillSource` (or a pre-built
    :class:`SkillCatalog`) instead of paths::

        plugins=[Skills(MyDatabaseSkillSource())]

    Either way the plugin contributes the ``load_skill`` / ``read_skill_file``
    tools and the Level-1 skill index (a system-prompt fragment).
    """

    name: str = "skills"

    def __init__(
        self,
        *sources: str | Path | SkillSource | SkillCatalog,
        usage_rules: str | None = None,
        filter: SkillFilter | None = None,
        name: str = "skills",
    ) -> None:
        self.catalog = _build_catalog(*sources, usage_rules=usage_rules, filter=filter)
        # Identity within an agent (see lovia.plugins.Plugin). Defaults to the
        # type name; override to mount two Skills plugins on one agent.
        self.name = name

    async def setup(self) -> PluginInstance:
        return PluginInstance(
            tools=self.catalog.tools(),
            instructions=await self.catalog.instructions() or None,
        )
