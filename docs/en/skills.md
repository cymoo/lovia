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
    model="<model>",
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

The directory source is live: listings re-scan the roots, bodies are read
lazily on every load, and a load for a just-created skill re-scans on the
miss — so adding, editing, or removing skills takes effect without
restarting anything.

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
- **`filter`** (any `SkillFilter` — a predicate) receives each skill's
  `SkillMetadata` (`name`, `description`, `extra`) and returns `True` to
  keep it. It is a real boundary, not cosmetics: a filtered-out skill is
  invisible in the index *and* unloadable by the tools.

Skill-layer failures raise `SkillsError` (with `skill_name`/`path`/`hint`);
unknown names raise its `SkillNotFoundError` subclass, which carries the
visible names. Inside the tools both are caught and returned to the model
as plain error strings instead.

## Custom backends

Directories are one source; the seams underneath are public:

- **`SkillSource`** — the storage protocol: `async list_skills() ->
  list[SkillMetadata]` plus `async load_skill(name) -> Skill` (raising
  `SkillNotFoundError` for unknown names). Implement it to serve skills
  from a database, an API, or an object store. Sources are expected to
  return *current* truth on every call — there is deliberately no
  reload/refresh seam to implement or invalidate.
  `DirectorySkillSource(*roots)` is the built-in one: it re-scans its
  directories per listing and self-heals on a load miss.
- **`SkillCatalog`** — a source plus its rules/filter, with the
  `instructions()` and `tools()` the plugin uses, and
  `list_skills()`/`load_skill()` for programmatic access. Build one
  directly (`SkillCatalog.from_dir(...)`, or wrap your source) when you
  want that access or to share a configured catalog:

```python
from lovia.plugins import SkillCatalog, Skills

catalog = SkillCatalog(MyDbSkillSource(), usage_rules="…")
agent = Agent(..., plugins=[Skills(catalog)])
```

(Passing a `SkillCatalog` together with `usage_rules=`/`filter=` on
`Skills` is rejected — configure them on the catalog.)

## Safety measures

- **Path traversal is blocked**: `read_skill_file` resolves the target and
  requires it to stay inside the skill's directory; skill names reject `/`,
  `\`, and `..` outright.
- **Loaded content is framed as data**: both tools wrap what they return
  in BEGIN/END reference-material markers (with embedded fakes
  neutralized), and the tool descriptions — sent once, prompt-cacheable —
  tell the model to treat that content as reference material. Instructions
  in a skill file are therefore weaker than your system prompt, and output
  is truncated at 100k chars.
- **The index cannot be reshaped**: when rendered into the system prompt,
  descriptions and extra frontmatter are collapsed to a single line and
  extra values are capped, so frontmatter cannot inject prompt structure.

## Sharp edges

- **Skill file IO bypasses the workspace ACL.** `load_skill` and
  `read_skill_file` do their own reads — a skill directory outside the
  [workspace](workspace.md) root, or matching `denied_paths`, still loads.
  Treat skill directories as trusted content; only *executing* a bundled
  script goes through the workspace shell policy.
- **Descriptions are the routing surface.** A vague description means the
  model loads the skill never (or always). Write it like a tool
  description: task-shaped, concrete, with trigger words.
- **The prompt index refreshes per run.** A skill added at runtime is
  loadable immediately (`load_skill` re-scans on a miss — handy when the
  agent just wrote the skill itself), but it appears in the system-prompt
  index on the next run, not mid-conversation.

## See also

- [Plugins](plugins.md) — the mechanism skills are built on
- [Memory](memory.md) — for knowledge the *agent* accumulates, rather than
  knowledge you author
- Example: [`22_skills.py`](../../examples/22_skills.py), sample skill:
  [`examples/skills/refund-policy/`](../../examples/skills/refund-policy/)
