# Skills

Team knowledge — policies, runbooks, style guides — doesn't belong in a
system prompt that every request pays for. A **skill** is a reusable
instruction bundle the model discovers cheaply and loads only when needed,
following the Agent Skills convention (`SKILL.md` + supporting files).

```python
from lovia import Agent, Skills

agent = Agent(
    name="support",
    instructions="Help customers using the right policy.",
    model="openai:gpt-5.5",
    plugins=[Skills("./skills")],
)
```

## Progressive disclosure

Skills cost context in three deliberate steps:

1. **The index** — always in the system prompt: one line per skill
   (`` `name` — description``, plus any extra frontmatter), followed by
   usage rules. This is all a skill costs until it's needed.
2. **`load_skill(name)`** — a plugin-provided tool that returns the full
   `SKILL.md` body when the model decides a skill applies.
3. **`read_skill_file(name, relpath)`** — reads a referenced file
   (`references/refund-tiers.md`, a script, a template) verbatim.

Bodies are read lazily from disk on every load — never cached — so editing
a skill takes effect on the next call without restarting anything.

## Anatomy of a skill

A skill is a directory holding `SKILL.md` with YAML frontmatter, plus
optional support files:

```
skills/
└── refund-policy/
    ├── SKILL.md
    ├── references/     # docs loaded on demand
    ├── scripts/        # executables the skill may mention
    └── assets/         # templates, fixtures
```

```markdown
---
name: refund-policy
description: How to evaluate and process refund requests, tier by tier.
---

# Refund policy

When a customer asks for a refund, first determine the tier...
See [references/refund-tiers.md](references/refund-tiers.md) for the table.
```

- `name` — `[a-zA-Z0-9]` segments joined by `-`/`_`, max 64 chars; falls
  back to the directory name when omitted.
- `description` — required, max 1024 chars. It is the model's routing
  signal, so say *when to use the skill*, not just what it is.
- Any other frontmatter keys land in `extra` and are shown in the index —
  teams use this for tags, owners, versions.

A skill with a malformed `SKILL.md` is skipped with a warning at scan time;
the rest of the catalog still loads.

## Configuration

```python
Skills("./skills", "./team-skills")                # several directories, scanned in order
Skills("./skills", usage_rules="Load at most one skill per reply.")
Skills("./skills", filter=lambda meta: "internal" not in meta.extra.get("tags", []))
```

- **Multiple directories** combine into one catalog; on a duplicate skill
  name the first occurrence wins (later ones are logged and skipped).
- **`usage_rules`** replaces the default usage block appended after the
  index; pass `""` to omit rules entirely.
- **`filter`** receives each skill's `SkillMetadata` (`name`,
  `description`, `extra`) and returns `True` to keep it. It is a real
  boundary, not cosmetics: a filtered-out skill is invisible in the index
  *and* unloadable by the tools.

## Custom backends

Directories are one source; the seams underneath are public:

- **`SkillSource`** — the storage protocol: a `metadata` property listing
  `SkillMetadata`, and `async load(name) -> Skill`. Implement it to serve
  skills from a database, an API, or an object store.
  `LocalDirSkillSource(*roots)` is the built-in one (with a `rescan()` for
  long-lived processes).
- **`SkillCategory`** — a source plus its rules/filter, with the
  `instructions()` and `tools()` the plugin uses. Build one directly
  (`SkillCategory.from_dir(...)`, or wrap your source) when you want
  programmatic access or to share a configured catalog:

```python
from lovia.plugins import SkillCategory, Skills

catalog = SkillCategory(MyDbSkillSource(), usage_rules="…")
agent = Agent(..., plugins=[Skills(catalog)])
```

(Passing a `SkillCategory` together with `usage_rules=`/`filter=` on
`Skills` is rejected — configure them on the category.)

## Safety measures

- **Path traversal is blocked**: `read_skill_file` resolves the target and
  requires it to stay inside the skill's directory; skill names reject `/`,
  `\`, and `..` outright.
- **Loaded content is framed as data**: `load_skill` wraps the body in
  BEGIN/END reference-material markers (with body-embedded fakes
  neutralized) so instructions in a skill file are weaker than your system
  prompt, and output is truncated at 100k chars.

## Sharp edges

- **Skill file IO bypasses the workspace ACL.** `load_skill` and
  `read_skill_file` do their own reads — a skill directory outside the
  [workspace](workspace.md) root, or matching `denied_paths`, still loads.
  Treat skill directories as trusted content; only *executing* a bundled
  script goes through the workspace shell policy.
- **Descriptions are the routing surface.** A vague description means the
  model loads the skill never (or always). Write it like a tool
  description: task-shaped, concrete, with trigger words.
- **The index is static per run.** New skills added to a directory appear
  on the next run (or after `rescan()` on a long-lived source), not
  mid-conversation.

## See also

- [Plugins](plugins.md) — the mechanism skills are built on
- [Memory](memory.md) — for knowledge the *agent* accumulates, rather than
  knowledge you author
- Example: [`22_skills.py`](../../examples/22_skills.py), sample skill:
  [`examples/skills/refund-policy/`](../../examples/skills/refund-policy/)
