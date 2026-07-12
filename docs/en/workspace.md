# Workspace

Give an agent files and a shell and you have a coding agent — and a safety
problem. `Workspace` adds file/shell tools scoped to a root directory and
governed by **one** `allow` / `ask` / `deny` policy that covers both paths
and commands, so there is a single place to reason about what the agent may
touch.

```python
from lovia import Agent
from lovia.workspace import CommandRule, Workspace

agent = Agent(
    name="coder",
    instructions="Make small, targeted code changes.",
    model="<model>",
    workspace=Workspace.local(
        ".",
        mode="coding",
        readable=("~/reference-docs",),      # extra read scope outside the root
        denied_paths=(".env*",),
        command_rules=(
            CommandRule("pytest", "allow"),
            CommandRule("rm -rf", "deny"),
        ),
    ),
)
```

The workspace contributes its tool bundle at run time, injects a generated
`## Workspace` section into the system prompt (derived from the policy, so
the prompt never promises more than the session enforces), and exposes its
live session to custom tools as `ctx.workspace`. `mode` takes a
`WorkspaceMode` (`"readonly"` / `"coding"` / `"trusted"`); denials raise
`PermissionDeniedError` and closed-session use raises
`WorkspaceClosedError` (both `WorkspaceError`, itself a `ToolError` — the
model sees them and adapts).

## Modes

`mode` picks a preset policy; reads **inside the root are always allowed**
(that is the point of having a root):

| Mode | Writes inside | Reads outside | Writes outside | Shell |
| --- | --- | --- | --- | --- |
| `readonly` | deny | deny | deny | none |
| `coding` (default) | allow | **ask** | deny | **ask** |
| `trusted` | allow | allow | **ask** | allow |

Refine any preset with `readable=` / `writable=` (grants), `denied_paths=`
(hard blocks), full `path_rules=` / `command_rules=`, or replace the whole
thing with `policy=WorkspacePolicy(...)` (mutually exclusive with the
shorthand knobs).

## The ACL

Three values, two enforcement points:

- **`deny` is enforced in the session** — the single choke point every file
  operation and command passes through, whether called by the built-in
  tools, your custom tools, or your own code. Denials raise
  `PermissionDeniedError` (a `ToolError` — the model sees it and adapts).
- **`ask` is resolved at the tool layer** — the built-in tools carry
  `needs_approval` predicates that consult the policy, so `ask` decisions
  surface through the standard
  [approval channel](tools.md#tool-approval), same as any gated
  tool.

**Path rules.** `PathRule(pattern, action, ops={"read","write"})`; patterns
are globs with three addressing forms — absolute/`~` (matches the resolved
path and its subtree), containing `/` (workspace-relative), or bare
(`.env*`: gitignore-style, matches a basename or any ancestor segment,
inside or outside the root). Precedence: `denied_paths` first, then the
first matching path rule, then the mode defaults.

**Command rules.** `CommandRule(pattern, action)` matches on
**word-boundary prefix**: `"git push"` matches `git push origin`, never
`git pushx`. Compound commands are split on `&&`, `||`, `;`, `|`, `&`; each
segment is judged and the **most restrictive** decision wins.

**Symlinks have no special case**: every path is resolved first (symlinks
followed, `~` expanded, relative anchored at the root) and judged by where
it *lands* — so a `.venv/bin/python` pointing at the system interpreter
just works when the policy allows that target, and a symlink escaping the
root is treated as the outside path it is.

## The tools

The bundle adapts to the policy (no write tools on a read-only workspace;
no `shell` when disabled):

| Tool | Notes | Parallel? |
| --- | --- | --- |
| `read_file` | 1-based `start`/`end` line paging | yes |
| `list_files` | glob filter, hidden-file toggle | yes |
| `grep_files` | regex, per-file and match caps | yes |
| `write_file` | `create_only=True` refuses overwrite | **barrier** |
| `edit_file` | exact-substring replace; fails on 0 or >1 matches unless `replace_all`; CRLF-tolerant | **barrier** |
| `shell` | `cwd` and per-call `timeout`; default timeout 300s | **barrier** |

Mutators default to `parallel=False`
([execution barriers](tools.md#parallel-execution-and-barriers)) so file
and process side effects never race within a turn; read-only tools stay
parallel.

Outputs are bounded at the tool layer by `WorkspaceLimits` (pass
`limits=WorkspaceLimits(...)`): `max_file_read_chars=50_000` per read (page
with `start`/`end`), `max_shell_output_chars=30_000` (head + tail kept),
plus byte caps for reads and grep, and result caps for list/grep. All
truncation is announced in the output.

Shell execution details worth knowing: commands run via the system shell
with a **minimal environment** by default (`PATH`, `HOME`, locale — secrets
are not passed through; `inherit_env=True` opts into the full environment,
`env=` adds specific variables), in a fresh process group; a timeout kills
the whole group and reports `timed_out=True`.

A virtualenv at the workspace root (`.venv` preferred, `venv` accepted) is
**auto-activated** for every command: its bin dir is prepended to `PATH`
and `VIRTUAL_ENV` is set, so `python`/`pip` resolve to the workspace's own
environment rather than the one lovia runs in. Detection is per command —
a venv the agent just created takes effect immediately — and only bites
when a real interpreter is inside (a directory merely *named* `venv`
doesn't). An explicit `env={"PATH": ...}` still wins. The workspace's
system-prompt fragment tells the model to create `.venv` before installing
Python packages rather than installing globally.

## The command guard

Static command rules can't see paths, so the session also **lexically**
extracts path claims from each command — redirect targets count as writes,
path-looking arguments as reads — and merges their path-ACL verdicts with
the static rule verdict, most-restrictive-wins. A command that names a
denied path (redirects included) is denied even when its binary is allowed.

The guard is **advisory and one-sided**: it cannot see `python -c` payloads
or `$(...)` substitutions, and a missed path falls back to the static
rules — it can add restrictions, never loosen them. The local shell still
runs as the host user. For *hard* isolation, the `ShellExecutor` seam
exists precisely to plug in an OS sandbox:

```python
class ShellExecutor(Protocol):
    async def run(self, command, *, cwd, env, timeout, policy, root) -> CommandResult: ...
```

An executor runs **after** the policy and approval gates (it decides *how*,
never *whether*) and can derive Seatbelt/bubblewrap/Landlock scopes from
the policy it receives. `Workspace.local(..., executor=my_sandbox)`.

## As a library, and from custom tools

The workspace is usable without an agent — the same session the tools use:

```python
async with Workspace.local("./project", mode="trusted").session() as ws:
    session = await ws.open()
    content = await session.read_text("hello.txt")
    matches = await session.grep("TODO", glob="*.py")
    result = await session.run("pytest -q")
```

Custom tools reach the active run's session as `ctx.workspace` and get the
same gate: `read_text` / `write_text` / `edit_text` / `list_files` /
`grep` / `run`, plus `decide_path(path, write=...)` and
`decide_command(command)` for tools that want to *check* before acting.
Deny raises; `ask` returns as a decision your tool's own `needs_approval`
predicate can honor. (`Workspace.local(...)` returns a `LocalWorkspace`
whose `open()` yields a `LocalWorkspaceSession`; the calls return typed
results — `FileContent`, `FileChange`, `EditResult`, `DirEntry`,
`GrepMatch`, `CommandResult` — and `PathRule.ops` takes `FileOp` values,
`"read"`/`"write"`.)

By default each run opens a fresh session and closes it at run end; the
`.session()` context manager above holds one open across runs
(`close_after_run=False`) when startup cost matters.

## Sharp edges

- **The command guard is not a sandbox.** It is an honest, lexical gate;
  interpreters and substitutions walk past it. Anything security-critical
  needs the `ShellExecutor` seam or an isolated host — the docs and the
  generated system prompt say the same thing on purpose.
- **`denied_paths` beats everything, including your own `readable=`
  grants** — check precedence before debugging "why can't it read that".
- **A cancelled `shell` call may leave work half-done.** The process group
  is killed on timeout, but a run-level cancel between approval and
  completion behaves like any [sync-tool cancellation](tools.md#sharp-edges):
  effects may land anyway; resume re-executes dangling calls.
- **[Skills](skills.md#sharp-edges) file IO bypasses this ACL** — skill
  directories are read with the plugin's own IO, not the workspace's.

## See also

- [Tool approval](tools.md#tool-approval) — where `ask` decisions land
- [Tools](tools.md) — barriers, truncation, error semantics
- Examples: [`19_workspace.py`](../../examples/19_workspace.py) (library),
  [`20_workspace_agent.py`](../../examples/20_workspace_agent.py) (coding agent)
