# 工作区

`Workspace` 为 Agent 提供文件和 Shell 工具，并用同一套 `allow` / `ask` / `deny` 策略
约束路径与命令。开放这些能力前，应先确定根目录和权限边界。

```python
from lovia import Agent
from lovia.workspace import CommandRule, Workspace

agent = Agent(
    name="coder",
    instructions="做小而明确的代码修改。",
    model="<model>",
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

工作区会在运行时提供一组工具，并向系统提示词注入自动生成的 `## Workspace` 章节。该章节根据策略生成，
因此不会向模型承诺当前 Session 无法完成的操作。自定义工具还可以通过 `ctx.workspace` 访问当前的工作区会话。
`mode` 接受 `WorkspaceMode`（`"readonly"` / `"coding"` / `"trusted"`）；拒绝操作会抛
`PermissionDeniedError`，关闭 session 后使用会抛 `WorkspaceClosedError`（两者都是
`WorkspaceError`，而它本身是 `ToolError`，所以模型会看到并调整）。

## 模式

`mode` 用来选择预设策略；所有模式都允许读取根目录内部：

| 模式 | 根目录内写入 | 根目录外读取 | 根目录外写入 | Shell |
| --- | --- | --- | --- | --- |
| `readonly` | deny | deny | deny | none |
| `coding`（默认） | allow | **ask** | deny | 只读命令和开发工具直接跑，不可撤回的问，其余**问** |
| `trusted` | allow | allow | **ask** | allow |

每种模式之下都有一条**底线**（baseline）：危险命令拒绝（`git push --force`、`rm -rf /`、`mkfs` 等），
凭据库禁止读写（`~/.ssh`、`~/.aws`、`*.pem` 等），写 `.git`、`.github/workflows`、`.vscode`、`.husky`、
`.lovia` 每次都问，工作区内的 `.env*` 读取前会问。所有*保护*都在底线里——它只会加严，用户层的
任何规则都不会无意间撤掉一条——去掉某一条就是不带它重新拼底线：
`baseline=DANGEROUS_COMMANDS + CREDENTIAL_PATHS + PROTECTED_CONFIG` 就让 agent 可以读 `.env`。

可以用 `readable=` / `writable=`（授权）、`denied_paths=`（硬阻断）、完整的 `path_rules=` /
`command_rules=`（你的规则排在预设规则前面）、`shell_default=`（`coding` 下传 `"allow"` 就是
“只拦危险的”姿态）、`baseline=`（用下面的规则包自己拼，或 `()` 关掉）细化预设；也可以用
`policy=WorkspacePolicy(...)` 完全替换（与简写配置项互斥）。

预设由 `lovia.workspace.rules` 里的规则包拼成，你也可以自己拼：`READ_ONLY`（`ls`、`du`、`stat`、`tree`、
`git status`、`git log` 等；Windows 上是 `dir`、`where` 等——只收录**不打印文件内容**的检查命令：每个
内容读取器都有自己不点名就读到文件的办法，`grep -r`、`sort --files0-from`、`git diff --no-index`、通配符，
所以 `cat` 之类留在灰区，逐文件判定的 `read_file` / `grep_files` 才是读取器；git 自己的命令打印的是*已跟踪*
内容，提交进仓库的密钥是仓库卫生问题，不归底线管）、`DEV_TOOLS`（`pytest`、`ruff`、`mypy`、`npm test`、
`cargo test` 等）、`IRREVERSIBLE`（`git push`、`git reset --hard`、`rm -r`、`sudo`、`docker` 等——要问），
以及底线的 `DANGEROUS_COMMANDS`、`CREDENTIAL_PATHS`、`PROTECTED_CONFIG`、`ENV_FILES`。放行 `DEV_TOOLS`
就是信任项目自己的代码在宿主上执行——这是 coding agent 的前提；这类工具的写入不经路径判定，底线管的
是模型亲手写的命令，不是沙箱。

## ACL

ACL 有三个决策值，分别作用于两个执行环节：

- **`deny` 在 session 层执行**：这是每个文件操作和命令都会经过的唯一关口，不管调用者是内置工具、
  你的自定义工具，还是你自己的代码。拒绝会抛 `PermissionDeniedError`（一种 `ToolError`，模型会看到并调整）。
- **`ask` 在工具层解决**：内置工具带 `permission` 谓词并咨询策略，所以 `ask` 判定会通过标准
  [审批通道](tools.md#工具审批)出现，和任何带门禁的工具一样，并带着策略给出的理由
  （`reads outside the workspace root require approval (~/notes)`）和授权范围（`read:~/notes`）。
  `deny` 在调用前就被拒绝并附上理由，模型不用执行就知道为什么。

**路径规则。** `PathRule(pattern, action, ops={"read","write"})`；pattern 是 glob，有三种寻址形式：
绝对路径/`~`（含 Windows 的 `C:/data`、`C:\data`；匹配解析后路径及其子树）、包含 `/`（工作区相对路径；
`**/.env*` 表示根目录内任意深度的该文件，根层也算），或纯文件名
（`.env*`：gitignore 风格，匹配 basename 或任意祖先片段，根内外都适用）。优先级：
`denied_paths` 最先，然后第一条匹配的 path rule，最后是 mode 默认。

**命令规则。** `CommandRule(pattern, action)` 按**词边界前缀**匹配：`"git push"` 会匹配
`git push origin`，不会匹配 `git pushx`。`flags=` 列出的词必须*另外*出现在该段的任意位置——
`CommandRule("git push", "deny", flags=("--force",))` 也能拦下 `git push origin main --force`；
单字母的 `-f` 被 `-fu` 满足，更长的旗标要整词出现，裸 `--` 之后的词不算。复合命令会按
`&&`、`||`、`;`、`|`、`&` 拆段；每段分别判断，取**最严格**决策。allow 规则只覆盖**字面参数**——
路径守卫能逐个判定的那种——所以三种情况会把一段封顶在 ask，不管它命中了什么规则：段首的 `VAR=value`
或薄包装（`env`、`nice`、`nohup`、`time`、`timeout N`——`PATH=…` 会改变实际运行的程序）；看起来像输出
选项的参数（`-o …`、`-oPATH`、`-ro`、`--output=…`、`--log-file`、`--junitxml`——命令可能写到它指向的
任何地方）；以及会被 shell 展开的参数（`cat *`、`cat $F`、`$(…)`——守卫看不到它展开成的路径）。
allow 降为 ask，deny 照样生效（`env git push --force`）；这种 ask 不可记忆。模式默认值
`shell_default="allow"` 不是规则、不为任何命令担保，所以 `trusted` 下 `cat *` 直接运行——这正是 trusted 的意思。

**底线**（`WorkspacePolicy.baseline`）单独判定，再与上面所有规则取最严格者合并，所以用户规则、
`command_decider` 或记住的授权都放松不了它。规则的 `reason` 就是审批卡片上显示的理由（拒绝时模型
也会读到）；`grant=False` 让前缀过宽的规则（`sudo`、`ssh`）不提供“总是允许”。

**批量遍历**（`list_files`、`grep_files`）会跳过任何非 `allow` 的条目——`ask` 没法在遍历中回答——
所以 `.env` 永远不会出现在搜索结果里；点名读取它会问。列出一个你批准过的*根目录外*目录时能看到
其中的条目（那次批准覆盖了整棵子树），但其中被规则门禁的文件仍会跳过。

**符号链接不作特殊处理**：每个路径都会先完成解析（跟随符号链接、展开 `~`，并将相对路径定位到根目录），
再根据最终指向的位置应用策略。因此，当 `.venv/bin/python` 指向系统解释器时，只要策略允许访问目标
即可执行；指向根目录之外的符号链接会按工作区外路径处理。

## 工具

工具包会按策略调整（readonly 工作区没有写工具；禁用 shell 时没有 `shell`）：

| 工具 | 说明 | 并发？ |
| --- | --- | --- |
| `read_file` | 1-based `start`/`end` 行分页；拒绝二进制 / 非 UTF-8 文件 | 是 |
| `list_files` | glob 过滤、是否包含隐藏文件 | 是 |
| `grep_files` | regex，每文件和总匹配数上限 | 是 |
| `write_file` | `create_only=True` 拒绝覆盖 | **屏障** |
| `edit_file` | 精确子串替换；0 或 >1 匹配时失败，除非 `replace_all`；兼容 CRLF | **屏障** |
| `shell` | `cwd` 和每次调用 `timeout`（默认 300s）；`background=true` 改为启动后台进程；可选 `description` —— 给用户看的一句话说明，UI 展示、策略忽略 | **屏障** |
| `read_process_output` | 增量读取后台进程输出及状态 | 是 |
| `kill_process` | 杀掉后台进程的整个 process group | **屏障** |

会修改状态的工具默认 `parallel=False`（[执行屏障](tools.md#并发执行与屏障)），避免文件和进程副作用在同一轮
里互相竞态；只读工具保持并发。

输出在工具层由 `WorkspaceLimits` 限制（传 `limits=WorkspaceLimits(...)`）：每次读取
`max_file_read_chars=50_000`（用 `start`/`end` 分页），shell 输出
`max_shell_output_chars=30_000`（保留头尾），另有读取/grep 字节上限，以及 list/grep 结果上限。
所有截断都会在输出中说明。

shell 执行细节：命令通过系统 shell 运行，默认使用**最小环境**（`PATH`、`HOME`、locale，不传 secrets；
`inherit_env=True` 才继承完整环境，`env=` 可加特定变量）。超时报告 `timed_out=True`，
并连同后代进程一起终止：POSIX 下终止新建的整个 process group，Windows 下终止整棵进程树
（`taskkill /T`）。前台命令在 shell 退出时也随之结束：如果它启动的进程（`server &`、守护进程）
两秒后仍占着输出，调用会停止读取、终止命令剩下的进程，并在 `stderr` 中注明；长期运行的进程请用
`background=true` 启动。重定向了输出的残留进程（`nohup x >log 2>&1 &`）不受跟踪，会继续运行，
`close()` 之后也不例外。

Windows 下还会保留宿主环境中已有的以下变量：系统路径（`SYSTEMROOT`、`WINDIR`、`SYSTEMDRIVE`）、
命令解释器（`COMSPEC`、`PATHEXT`）、临时目录（`TEMP`、`TMP`）、用户目录与用户名
（`USERPROFILE`、`USERNAME`、`HOMEDRIVE`、`HOMEPATH`、`APPDATA`、`LOCALAPPDATA`）和标准安装目录
（`PROGRAMDATA`、`PROGRAMFILES`、`PROGRAMFILES(X86)`），相当于 POSIX 下的 `HOME`/`USER`。
其中 Winsock 需要 `SYSTEMROOT` 才能加载 provider DLL；缺少它时，
仅创建 socket 就可能报 `WinError 10106`。前台和后台命令遵循相同的环境规则。
API key、代理配置和自定义 CA 变量仍需通过 `env=` 显式传入；
`lovia web --trust-env` 只控制模型 provider 的 HTTP 请求。

## 后台进程

`shell(command, background=true)` 会把命令作为 **session 拥有的后台进程**启动，并立即返回
process id —— 这是运行 dev server、watcher、长构建/长测试的方式：启动后用后续命令验证它
（用 `curl` 访问 server，用 `read_process_output` 看测试尾部输出）。启动经过与前台命令
**完全相同的策略与审批门禁**；backgrounding 绝不会让判定变松。

语义：

- stdout 和 stderr **合并**后写入每进程的有界缓冲；`read_process_output(process_id)` 返回
  自上次读取以来的新输出，附带状态（`running` / `exited` 及退出码 / `killed`）。两次读取之间
  只保留最新的 `max_shell_output_chars` 个字符——更早的未读输出被丢弃，丢弃会在结果中说明。
- 读取永远不会变成错误：轮询一个已退出的进程会报告退出码并排空剩余输出。*未知* id 才会报错，
  错误信息里列出当前存活的 id（没有单独的 list 工具）。
- `kill_process(process_id)` 杀掉整个 process group（包括子进程）并返回最后的输出尾部。
  后台进程没有超时；它们随 session 一起消亡（`close()` 收割所有 process group）。
- 每一轮都有一条临时**状态提醒**（与 todo 重新展示相同的视图注入机制——不落 transcript、
  不累积）：让运行中的进程保持在模型视野里，并持续通告某次退出，直到
  `read_process_output`/`kill_process` 把结果交付给模型为止。于是 dev server 崩溃后
  下一轮就会被注意到，无需轮询。
- 进程是**短暂的**：checkpoint 恢复不会还原它们。重启后 `read_process_output` 会明确说明，
  解法是照 transcript 里的启动命令重跑一次。
- 配置了自定义 `ShellExecutor` 时 `spawn` 会拒绝执行，而不是悄悄绕过其 sandbox
  （executor 的后台支持尚未接入）。

同一套能力也在 session 上供库用法和自定义工具使用：`spawn` / `read_process_output` /
`kill_process`，另有 `background_processes()`——被动状态列表（不消费任何输出），
供状态提醒和 UI 列表使用。

**session 的生命周期由服务层决定。**默认情况下 runner 每次 run 打开新 session、run 结束即关闭——
对一次性的 `Runner.run` 脚本正合适（run 就是整个对话）。持有长对话的服务层应该用
`LocalWorkspace.bind(session)`（或 `.session()` 上下文管理器）绑定一个自管的 session 来拉长作用域：
`lovia web` 给每个**聊天**绑定一个 session，所以某一轮启动的 dev server 在下一条消息到来时仍然活着，
直到聊天被删除或服务停止（Ctrl+C 会收割全部进程；`kill -9` 跳过清理，进程会成为孤儿）。

工作区根目录下的 virtualenv（优先 `.venv`，也识别 `venv`）会对每条命令**自动激活**：其 bin 目录被前置到
`PATH`、并设置 `VIRTUAL_ENV`，于是 `python`/`pip` 解析到工作区自己的环境，而不是 lovia 运行所在的那个。
检测按命令进行——agent 刚创建的 venv 立即生效——且只在目录里确实有解释器时才激活（仅仅叫 `venv`
的目录不会）。显式传入的 `env={"PATH": ...}` 仍然优先。工作区的 system prompt 片段会告诉模型：安装
Python 包之前先创建 `.venv`，永远不要装进全局环境。

可写 Workspace 的自动指令要求临时产物放入 `tmp/`，并遵循现有仓库布局；`tmp/` 不作为交付物提交。

## 命令执行控制

静态命令规则看不到路径，所以 session 还会从每条命令里**词法**提取路径声明：重定向目标算写入，
其余参数（包括 `id_rsa` 这样的裸文件名；命令词本身只在写成路径时才算）算读取。它会把这些路径 ACL
判断和静态命令判断合并，取最严格结果。命令只要提到被 deny 的路径（包括重定向），即使命令本身被 allow，
也会被 deny：`cat id_rsa` 和 `read_file("id_rsa")` 过的是同一条凭据规则。守卫不知道程序通过哪个
*选项*写文件；策略从另一头堵上：带输出样子选项的段永远不会直接放行（见上）。Windows 上反斜杠是路径分隔符，
不是转义符，所以 `C:\data\a.txt` 会按原样判断。

这个门禁是**启发式的，而且只会收紧权限**：它看不到 `python -c` 里的代码或 `$(...)` 命令替换，
漏掉的路径会退回到静态规则。它只能增加限制，不能放宽。本地 shell 仍然以宿主用户身份运行。
真正的强隔离请接入 `ShellExecutor` 这个扩展点，它正是为 OS sandbox 准备的：

```python
class ShellExecutor(Protocol):
    async def run(self, command, *, cwd, env, timeout, policy, root) -> CommandResult: ...
```

Executor 在策略检查和审批完成**之后**运行。它只决定命令**如何执行**，无权决定命令
**是否允许执行**。它可以根据收到的 policy 派生 Seatbelt、bubblewrap 或 Landlock 范围。用法：
`Workspace.local(..., executor=my_sandbox)`。

## 在代码和自定义工具中使用

工作区也可以脱离 Agent 单独使用，所得 Session 与内置工具使用的完全相同：

```python
async with Workspace.local("./project", mode="trusted").session() as ws:
    session = await ws.open()
    content = await session.read_text("hello.txt")
    matches = await session.grep("TODO", glob="*.py")
    result = await session.run("pytest -q")
```

自定义工具可以通过 `ctx.workspace` 访问当前运行的工作区 Session，所有操作仍会经过同一套权限检查：
`read_text` / `write_text` / `edit_text` / `list_files` / `grep` / `run` / `spawn` /
`read_process_output` / `kill_process`，以及
`decide_path(path, write=...)` 和 `decide_command(command)`，供工具在行动前检查。两者都返回
`Verdict`（`action`、`reason`、`scope`）；文件和命令方法遇到 `deny` 会抛异常，你自己的
`permission` 谓词可以把判定原样返回。
（`Workspace.local(...)` 返回 `LocalWorkspace`，其 `open()` 产出 `LocalWorkspaceSession`；
调用返回类型化结果：`FileContent`、`FileChange`、`EditResult`、`DirEntry`、`GrepMatch`、
`CommandResult`、`ProcessStart`、`ProcessOutput`；`PathRule.ops` 接受 `FileOp` 值
`"read"`/`"write"`。）

默认每次运行都会打开一个新 session，并在运行结束时关闭；上面的 `.session()` context manager 可以把一个
session 跨运行保持打开（`close_after_run=False`），适合启动成本重要的场景。

## 使用建议

- **命令门禁不是 sandbox。** 它是尽力而为的词法门禁；解释器和命令替换可以绕过它。任何安全关键场景
  都需要 `ShellExecutor` 或隔离主机。文档和生成的 system prompt 都会明确说明这一点。
- **`denied_paths` 胜过一切，包括你自己的 `readable=` 授权。** 调试“为什么读不了”前，先看优先级。
- **被取消的 `shell` 调用可能留下半完成状态。** 超时时会杀掉 process group，但运行级取消如果发生在审批后、
  完成前，就和任何[同步工具取消](tools.md#使用建议)一样：副作用可能仍然发生；恢复会重新执行悬空调用。
- **后台进程随 session 存亡。** `close()` 时被杀掉，checkpoint 恢复也不会还原——用
  `background=true` 启动的 dev server 应当被视为需要重启的东西，而不是会一直活着的东西。
  Session 本身活多久由服务层决定：默认随 Run 存亡；在 `lovia web` 中则随聊天存亡（见上文）。
- **[Skills](skills.md#使用建议) 文件 IO 不受此 ACL 管理**：Skill 目录由插件自行读取，不经过工作区。

## 延伸阅读

- [工具审批](tools.md#工具审批)：`ask` 决策去哪里
- [工具](tools.md)：屏障、截断、错误语义
- 示例：[`19_workspace.py`](../../examples/19_workspace.py)（库用法），
  [`20_workspace_agent.py`](../../examples/20_workspace_agent.py)（coding agent）
