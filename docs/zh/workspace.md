# 工作区

给 agent 接上文件系统和 shell，它就能做代码修改，也立刻带来安全问题。`Workspace` 会添加作用域限制在
根目录内的文件/shell 工具，并用**同一套** `allow` / `ask` / `deny` 策略管理路径和命令。这样你只需要
在一个地方判断 agent 能碰什么。

```python
from lovia import Agent
from lovia.workspace import CommandRule, Workspace

agent = Agent(
    name="coder",
    instructions="做小而明确的代码修改。",
    model="glm-5.2",
    workspace=Workspace.local(
        ".",
        mode="coding",
        readable=("~/reference-docs",),      # 根目录外的额外读取范围
        denied_paths=(".env*",),
        command_rules=(
            CommandRule("pytest", "allow"),
            CommandRule("rm -rf", "deny"),
        ),
    ),
)
```

工作区会在运行时贡献工具包，向 system prompt 注入生成的 `## Workspace` 章节（根据策略生成，所以
prompt 不会承诺 session 做不到的事），并把实时 session 暴露给自定义工具为 `ctx.workspace`。
`mode` 接受 `WorkspaceMode`（`"readonly"` / `"coding"` / `"trusted"`）；拒绝操作会抛
`PermissionDeniedError`，关闭 session 后使用会抛 `WorkspaceClosedError`（两者都是
`WorkspaceError`，而它本身是 `ToolError`，所以模型会看到并调整）。

## 模式

`mode` 选择预设策略；根目录内的读取**始终允许**，这正是有根目录的意义：

| 模式 | 根目录内写入 | 根目录外读取 | 根目录外写入 | Shell |
| --- | --- | --- | --- | --- |
| `readonly` | deny | deny | deny | none |
| `coding`（默认） | allow | **ask** | deny | **ask** |
| `trusted` | allow | allow | **ask** | allow |

可以用 `readable=` / `writable=`（授权）、`denied_paths=`（硬阻断）、完整的 `path_rules=` /
`command_rules=` 细化预设；也可以用 `policy=WorkspacePolicy(...)` 完全替换（与简写配置项互斥）。

## ACL

三个取值，两个执行点：

- **`deny` 在 session 层执行**：这是每个文件操作和命令都会经过的唯一关口，不管调用者是内置工具、
  你的自定义工具，还是你自己的代码。拒绝会抛 `PermissionDeniedError`（一种 `ToolError`，模型会看到并调整）。
- **`ask` 在工具层解决**：内置工具带 `needs_approval` 谓词并咨询策略，所以 `ask` 决策会通过标准
  [审批通道](human-in-the-loop.md#工具审批)出现，和任何带门禁的工具一样。

**路径规则。** `PathRule(pattern, action, ops={"read","write"})`；pattern 是 glob，有三种寻址形式：
绝对路径/`~`（匹配解析后路径及其子树）、包含 `/`（工作区相对路径），或裸名
（`.env*`：gitignore 风格，匹配 basename 或任意祖先片段，根内外都适用）。优先级：
`denied_paths` 最先，然后第一条匹配的 path rule，最后是 mode 默认。

**命令规则。** `CommandRule(pattern, action)` 按**词边界前缀**匹配：`"git push"` 会匹配
`git push origin`，不会匹配 `git pushx`。复合命令会按 `&&`、`||`、`;`、`|`、`&` 拆段；
每段分别判断，取**最严格**决策。

**symlink 没有特例**：每个路径都会先解析（跟随 symlink、展开 `~`、相对路径锚到 root），然后按它
**落到哪里**判断。所以 `.venv/bin/python` 指向系统解释器时，只要策略允许目标就能用；逃出根目录的
symlink 会按根外路径处理。

## 工具

工具包会按策略调整（readonly 工作区没有写工具；禁用 shell 时没有 `shell`）：

| 工具 | 说明 | 并发？ |
| --- | --- | --- |
| `read_file` | 1-based `start`/`end` 行分页 | 是 |
| `list_files` | glob 过滤、是否包含隐藏文件 | 是 |
| `grep_files` | regex，每文件和总匹配数上限 | 是 |
| `write_file` | `create_only=True` 拒绝覆盖 | **屏障** |
| `edit_file` | 精确子串替换；0 或 >1 匹配时失败，除非 `replace_all`；兼容 CRLF | **屏障** |
| `shell` | `cwd` 和每次调用 `timeout`；默认超时 300s | **屏障** |

会修改状态的工具默认 `parallel=False`（[执行屏障](tools.md#并发执行与屏障)），避免文件和进程副作用在同一轮
里互相竞态；只读工具保持并发。

输出在工具层由 `WorkspaceLimits` 限制（传 `limits=WorkspaceLimits(...)`）：每次读取
`max_file_read_chars=50_000`（用 `start`/`end` 分页），shell 输出
`max_shell_output_chars=30_000`（保留头尾），另有读取/grep 字节上限，以及 list/grep 结果上限。
所有截断都会在输出中说明。

shell 执行细节：命令通过系统 shell 运行，默认使用**最小环境**（`PATH`、`HOME`、locale，不传 secrets；
`inherit_env=True` 才继承完整环境，`env=` 可加特定变量），运行在新的 process group 中；超时会杀掉整个
process group，并报告 `timed_out=True`。

工作区根目录下的 virtualenv（优先 `.venv`，也认 `venv`）会对每条命令**自动激活**：其 bin 目录被前置到
`PATH`、并设置 `VIRTUAL_ENV`，于是 `python`/`pip` 解析到工作区自己的环境，而不是 lovia 运行所在的那个。
检测按命令进行——agent 刚创建的 venv 立即生效——且只在目录里真有解释器时才激活（仅仅*名叫* `venv`
的目录不会）。显式传入的 `env={"PATH": ...}` 仍然优先。工作区的 system-prompt 片段会告诉模型：安装
Python 包之前先创建 `.venv`，永远不要装进全局环境。

## 命令门禁

静态命令规则看不到路径，所以 session 还会从每条命令里**词法**提取路径声明：重定向目标算写入，
看起来像路径的参数算读取。它会把这些路径 ACL 判断和静态命令判断合并，取最严格结果。命令只要提到
被 deny 的路径（包括重定向），即使 binary 被 allow，也会被 deny。

这个门禁是**启发式的，而且只会收紧权限**：它看不到 `python -c` 里的代码或 `$(...)` 命令替换，
漏掉的路径会退回到静态规则。它只能增加限制，不能放宽。local shell 仍然以宿主用户身份运行。
真正的强隔离请接入 `ShellExecutor` 这个扩展点，它正是为 OS sandbox 准备的：

```python
class ShellExecutor(Protocol):
    async def run(self, command, *, cwd, env, timeout, policy, root) -> CommandResult: ...
```

executor 在策略和审批门禁**之后**运行（它决定**怎么**执行，不决定**能不能**执行），可以根据收到的
policy 派生 Seatbelt/bubblewrap/Landlock 范围。用法：
`Workspace.local(..., executor=my_sandbox)`。

## 作为库，以及在自定义工具里使用

工作区可以脱离 agent 使用，也就是工具使用的同一套 session：

```python
async with Workspace.local("./project", mode="trusted").session() as ws:
    session = await ws.open()
    content = await session.read_text("hello.txt")
    matches = await session.grep("TODO", glob="*.py")
    result = await session.run("pytest -q")
```

自定义工具通过 `ctx.workspace` 拿到当前运行的 session，并经过同一套门禁：
`read_text` / `write_text` / `edit_text` / `list_files` / `grep` / `run`，以及
`decide_path(path, write=...)` 和 `decide_command(command)`，供工具在行动前检查。deny 会抛异常；
`ask` 会作为决策返回，让你自己的 `needs_approval` 谓词处理。
（`Workspace.local(...)` 返回 `LocalWorkspace`，其 `open()` 产出 `LocalWorkspaceSession`；
调用返回类型化结果：`FileContent`、`FileChange`、`EditResult`、`DirEntry`、`GrepMatch`、
`CommandResult`；`PathRule.ops` 接受 `FileOp` 值 `"read"`/`"write"`。）

默认每次运行都会打开一个新 session，并在运行结束时关闭；上面的 `.session()` context manager 可以把一个
session 跨运行保持打开（`close_after_run=False`），适合启动成本重要的场景。

## 容易踩的点

- **命令门禁不是 sandbox。** 它是尽力而为的词法门禁；解释器和命令替换可以绕过它。任何安全关键场景
  都需要 `ShellExecutor` 或隔离主机。文档和生成的 system prompt 都刻意这样说。
- **`denied_paths` 胜过一切，包括你自己的 `readable=` 授权。** 调试“为什么读不了”前，先看优先级。
- **被取消的 `shell` 调用可能留下半完成状态。** 超时时会杀掉 process group，但运行级取消如果发生在审批后、
  完成前，就和任何[同步工具取消](tools.md#容易踩的点)一样：副作用可能仍然发生；恢复会重新执行悬空调用。
- **[Skills](skills.md#容易踩的点) 文件 IO 绕过这个 ACL**：skill 目录由插件自己的 IO 读取，不走工作区。

## 延伸阅读

- [人工介入](human-in-the-loop.md)：`ask` 决策去哪里
- [工具](tools.md)：屏障、截断、错误语义
- 示例：[`19_workspace.py`](../../examples/19_workspace.py)（库用法），
  [`20_workspace_agent.py`](../../examples/20_workspace_agent.py)（coding agent）
