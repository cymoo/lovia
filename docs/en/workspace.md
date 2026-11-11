# Workspace

`Workspace` adds file and Shell tools under one `allow` / `ask` / `deny` policy
for paths and commands. Define the root and permission boundary before exposing
these capabilities to an Agent.

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

`mode` picks a preset policy; every mode allows reads inside the root:

| Mode | Writes inside | Reads outside | Writes outside | Shell |
| --- | --- | --- | --- | --- |
| `readonly` | deny | deny | deny | none |
| `coding` (default) | allow | **ask** | deny | the irreversible asks with a reason, the rest **asks** (rememberable per chat) |
| `trusted` | allow | allow | **ask** | allow |

Under every mode the **baseline** holds: dangerous commands are denied
(`git push --force`, `rm -rf /`, `mkfs`, …), credential stores are off-limits
(`~/.ssh`, `~/.aws`, `*.pem`, …), and writes to `.git`, `.github/workflows`,
`.vscode`, `.husky`, `.lovia` ask every time, and `.env*` files inside the
workspace ask before being read. Every *protection* is baseline — it only
ever tightens, so nothing in the user layer can undo one by accident; inside
it every matching rule counts and the strictest wins, so bundles compose in
any order without shadowing each other — and
dropping one is composing the baseline without it: `baseline=
DANGEROUS_COMMANDS + CREDENTIAL_PATHS + PROTECTED_CONFIG` lets the agent read
`.env` files.

Refine any preset with `readable=` / `writable=` (grants), `denied_paths=`
(hard blocks), full `path_rules=` / `command_rules=` (yours come before the
preset's), `shell_default=` (`"allow"` on `coding` is the "only the dangerous
is stopped" posture), `baseline=` (compose from the bundles below, or `()`),
or replace the whole thing with `policy=WorkspacePolicy(...)` (mutually
exclusive with the shorthand knobs).

`coding` vouches for **no command** itself: the path guard is lexical and
cannot audit a program's options, so which commands run unprompted is a
choice — the deployer's, by opting in a bundle (`lovia web` opts into
`READ_ONLY` + `DEV_TOOLS`; Settings → Permissions changes that), or the user's, per chat, on
the approval card ("always allow `ls`"; the scope offered for an unlisted
command is its command word, and a chained command offers one per command). The bundles live in `lovia.workspace.rules`:
`READ_ONLY` (`ls`, `du`, `stat`, `tree`, `git status`,
`git log`, … ; `dir`, `where`, … on Windows — inspection that prints **no
file contents**: every content reader has its own way to reach a file it
never names, `grep -r`, `sort --files0-from`, `git diff --no-index`, a glob,
so `cat` & co. stay gray and `read_file` / `grep_files`, which judge every
file they open, are the readers; git's own commands print *tracked* content,
and a secret committed to the repository is repository hygiene, not the
baseline's hand — except `git diff`, which outside a repository diffs any two
paths as if `--no-index` were spelled, so it is gray too), `DEV_TOOLS` (`pytest`, `ruff`, `mypy`, `npm test`,
`cargo test`, …), `IRREVERSIBLE` (`git push`, `git reset --hard`, `rm -r`,
`sudo`, `docker`, … — asked), and the baseline's `DANGEROUS_COMMANDS`,
`CREDENTIAL_PATHS`, `PROTECTED_CONFIG`, `ENV_FILES`. Allowing `DEV_TOOLS` is
trusting the project's own code to run on the host — the premise of a coding
agent; such tools write wherever their job takes them, and the baseline is
about the model's own hand, not a sandbox.

## The ACL

Three values, two enforcement points:

- **`deny` is enforced in the session** — the single choke point every file
  operation and command passes through, whether called by the built-in
  tools, your custom tools, or your own code. Denials raise
  `PermissionDeniedError` (a `ToolError` — the model sees it and adapts).
- **`ask` is resolved at the tool layer** — the built-in tools carry
  `permission` predicates that consult the policy, so `ask` verdicts
  surface through the standard
  [approval channel](tools.md#tool-approval), same as any gated
  tool, carrying the policy's reason (`Outside the workspace root
  (~/notes).`; none for a command no rule names — being asked says it all)
  and its grant scopes (`read:~/notes`). A `deny`
  is refused before the call with its reason, so the model learns why
  without anything running.

**Path rules.** `PathRule(pattern, action, ops={"read","write"})`; patterns
are globs with three addressing forms — absolute/`~` (including `C:/data` or
`C:\data` on Windows; matches the resolved path and its subtree), containing
`/` (workspace-relative; `**/.env*` names a file at any depth inside the
root, the root itself included), or bare
(`.env*`: gitignore-style, matches a basename or any ancestor segment,
inside or outside the root). Precedence: `denied_paths` first, then the
first matching path rule, then the mode defaults.

**Command rules.** `CommandRule(pattern, action)` matches on
**word-boundary prefix**: `"git push"` matches `git push origin`, never
`git pushx`. `flags=` names words that must *also* appear anywhere in the
segment — `CommandRule("git push", "deny", flags=("--force",))` stops `git
push origin main --force` too; a one-letter `-f` is satisfied by `-fu`,
anything longer must appear as that word, and words after a bare `--` never
count. Compound commands are split on `&&`, `||`, `;`, `|`, `&`; each segment
is judged and the **most restrictive** decision wins. What the shell keeps
for itself (a redirection and its target, parentheses, `!` / `if` / `while` /
`do` before the command) is not the program's: `(ls)` is `ls`, and `! git
push --force` or `git > /dev/null push --force` meets the deny. That is the
whole of it — a rule stops the spellings it names after this; a model bent
on hiding a command has `bash -c`, and under `coding` every spelling the
rules miss still asks, with the spelling on the card. An allow rule covers
**literal arguments only** — what the path guard can judge one by one — so
three things cap a segment at `ask` whatever rule it matched: a leading
`VAR=value` or thin wrapper (`command`, `exec`, `env`, `nice`, `nohup`,
`time`, `timeout N` — `PATH=…` changes which program runs); an argument that looks like an output
option (`-o …`, `-oPATH`, `-ro`, `--output=…`, `--log-file`, `--junitxml` —
the command may write wherever it points); and an argument the shell
expands (`cat *`, `cat $F`, `$(…)` — the guard never sees the paths it
becomes). An allow becomes an ask, a deny still bites (`env git push
--force`); the ask is not grantable. The mode default `shell_default="allow"`
is not a rule vouching for anything, so `trusted` runs `cat *` unprompted —
which is its point.

**The baseline** (`WorkspacePolicy.baseline`) is judged on its own — every
matching rule, strictest wins — and merged most-restrictive with everything
above, so no user rule, `command_decider` or remembered approval loosens it,
and no baseline rule shadows another. A rule's `reason` is what the approval
card shows (and the model reads on a denial); `grant=False` withholds the
"always allow" option where the prefix is a whole world (`sudo`, `ssh`).

**Bulk walks** (`list_files`, `grep_files`) skip anything short of `allow`
— an `ask` cannot be answered mid-walk — so a `.env` never surfaces in a
search; reading it by name asks. Listing an *outside* directory you approved
shows its entries (that approval covered the subtree), but a rule-gated file
inside it is still skipped.

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
| `read_file` | 1-based `start`/`end` line paging; refuses binary / non-UTF-8 files | yes |
| `list_files` | glob filter, hidden-file toggle | yes |
| `grep_files` | regex, per-file and match caps | yes |
| `write_file` | `create_only=True` refuses overwrite | **barrier** |
| `edit_file` | exact-substring replace; fails on 0 or >1 matches unless `replace_all`; CRLF-tolerant | **barrier** |
| `shell` | `cwd` and per-call `timeout` (default 300s); `background=true` starts a background process instead; optional `description` — a user-facing one-liner the UI shows, ignored by policy | **barrier** |
| `read_process_output` | incremental output + status of a background process | yes |
| `kill_process` | kill a background process's whole group | **barrier** |

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
`env=` adds specific variables). A timeout reports `timed_out=True` and kills
the command with its descendants: the fresh process group on POSIX, the
process tree (`taskkill /T`) on Windows. A foreground command also ends
when its shell exits: if something it started (`server &`, a daemon) still
holds the output open two seconds later, the call stops reading, kills what
is left of the command, and notes it in `stderr` — run long-lived processes
with `background=true`. A leftover that redirected its output
(`nohup x >log 2>&1 &`) is not tracked: it keeps running, past `close()` too.

On Windows, the minimal environment also keeps these variables when present
in the host environment: system paths (`SYSTEMROOT`, `WINDIR`, `SYSTEMDRIVE`),
the shell (`COMSPEC`, `PATHEXT`), temp directories (`TEMP`, `TMP`), the user
profile (`USERPROFILE`, `USERNAME`, `HOMEDRIVE`, `HOMEPATH`, `APPDATA`,
`LOCALAPPDATA`), and standard install folders (`PROGRAMDATA`, `PROGRAMFILES`,
`PROGRAMFILES(X86)`) — the counterparts of `HOME`/`USER` on POSIX.
In particular, Winsock needs `SYSTEMROOT` to load its provider DLLs; removing
it can cause `WinError 10106` even when creating a socket. Foreground and
background commands use the same environment rules. API keys, proxy settings,
and custom CA variables still require explicit `env=` configuration;
`lovia web --trust-env` only controls model-provider HTTP requests.

## Background processes

`shell(command, background=true)` starts the command as a **session-owned
background process** and returns a process id immediately — the way to run
dev servers, watchers, and long builds or test runs, then verify them with
follow-up commands (`curl` against the server, `read_process_output` for the
test tail). The start is judged by the **same policy and approval
gate** as a foreground command; backgrounding never softens the verdict.

Semantics:

- stdout and stderr are **merged** and spooled to a bounded per-process
  buffer; `read_process_output(process_id)` returns what arrived since the
  last read, plus status (`running` / `exited` with code / `killed`).
  Between reads only the newest `max_shell_output_chars` are kept — older
  unread output is dropped and the drop is announced.
- Reads never turn into errors: polling an exited process reports its exit
  code and drains the remainder. An *unknown* id raises with the live ids
  listed in the message (there is no separate list tool).
- `kill_process(process_id)` kills the whole process group (children
  included) and returns the final tail. No timeout applies to background
  processes; they die with the session (`close()` reaps every group).
- Every turn, a transient **status reminder** (same view-injection
  mechanism as the todo re-show — never persisted, never accumulating)
  keeps running processes in the model's view and announces an exit until
  a `read_process_output`/`kill_process` delivers it. So a crashed dev
  server is noticed on the next turn without polling.
- Processes are **ephemeral**: a checkpoint resume does not restore them.
  After a restart, `read_process_output` says so and the fix is to re-run
  the start command from the transcript.
- `spawn` refuses to run under a custom `ShellExecutor` rather than
  silently bypassing its sandbox (background support for executors is not
  wired yet).

The same surface is available on the session for library use and custom
tools: `spawn` / `read_process_output` / `kill_process`, plus
`background_processes()` — a passive status list (nothing consumed) that
feeds the reminder and suits UI listings.

**Session lifetime is the serving layer's dial.** By default the runner
opens a session per run and closes it at the run's end — fine for one-shot
`Runner.run` scripts, where the run *is* the conversation. A serving layer
that holds conversations open should scope the session wider by binding a
caller-owned one via `LocalWorkspace.bind(session)` (or the
`.session()` context manager): `lovia web` binds one session per **chat**
(for a custom backend too),
so a dev server started in one turn is still up when the next message
arrives, and dies when the chat is deleted or the server shuts down
(Ctrl+C; a hard `kill -9` skips teardown and orphans processes). A custom
backend's session binds the same way:
`WorkspaceSessionBinding(config, session)`. Between runs,
`session.set_policy(policy)` changes what a held session allows without
closing it.

A virtualenv at the workspace root (`.venv` preferred, `venv` accepted) is
**auto-activated** for every command: its bin dir is prepended to `PATH`
and `VIRTUAL_ENV` is set, so `python`/`pip` resolve to the workspace's own
environment rather than the one lovia runs in. Detection is per command —
a venv the agent just created takes effect immediately — and only bites
when a real interpreter is inside (a directory merely *named* `venv`
doesn't). An explicit `env={"PATH": ...}` still wins. The workspace's
system-prompt fragment tells the model to create `.venv` before installing
Python packages rather than installing globally.

For writable workspaces, the automatic instructions place temporary work in
`tmp/`, follow the existing repository layout, and never treat `tmp/` as a
deliverable.

## The command guard

Static command rules can't see paths, so the session also **lexically**
extracts path claims from each command — redirect targets count as writes,
every other argument (bare names like `id_rsa` included; the command word
itself only when spelled as a path) as a read — and merges their path-ACL
verdicts with the static rule verdict, most-restrictive-wins. A command that
names a denied path (redirects included) is denied even when its binary is
allowed: `cat id_rsa` meets the same credential rule as
`read_file("id_rsa")`. The guard does not know which *option* a program
writes through; the policy covers that from the other side by never
allowing a segment that names an output-looking option outright (above).
On Windows a backslash is a path separator, not an escape, so `C:\data\a.txt`
is judged as written.

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
same gate: `read_text` / `read_bytes` / `write_text` / `write_bytes` /
`edit_text` / `list_files` /
`grep` / `run` / `spawn` / `read_process_output` / `kill_process`, plus
`decide_path(path, write=...)` and
`decide_command(command)` for tools that want to *check* before acting.
Both return a `Verdict` (`action`, `reason`, `scopes`); the file and shell
methods raise on `deny`, and your tool's own `permission` predicate can
return the verdict as is. (`Workspace.local(...)` returns a `LocalWorkspace`
whose `open()` yields a `LocalWorkspaceSession`; the calls return typed
results — `FileContent`, `FileChange`, `EditResult`, `DirEntry`,
`GrepMatch`, `CommandResult`, `ProcessStart`, `ProcessOutput` — and
`PathRule.ops` takes `FileOp` values, `"read"`/`"write"`.)

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
- **Background processes are session-scoped.** They die on `close()` and
  are not restored by a checkpoint resume — treat a dev server started with
  `background=true` as something to re-start, not something that survives.
  How long the session itself lives is the serving layer's choice: per run
  by default, per chat in `lovia web` (see above).
- **[Skills](skills.md#sharp-edges) file IO bypasses this ACL** — skill
  directories are read with the plugin's own IO, not the workspace's.

## See also

- [Tool approval](tools.md#tool-approval) — where `ask` decisions land
- [Tools](tools.md) — barriers, truncation, error semantics
- Examples: [`19_workspace.py`](../../examples/19_workspace.py) (library),
  [`20_workspace_agent.py`](../../examples/20_workspace_agent.py) (coding agent)
