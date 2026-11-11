# 远程工作区：连到已有容器和 SSH 机器

> 状态：库这一层已实现并发布（PR 1–6：#392–#397，v0.9.167–172）；web 接入分三步，7a（服务端绑定）随本文一起提交，7b（界面）和 7c（Files、上传、附件）未做。实现与原文不同的地方记在 §7 和 §8，以那两节为准 · 2026-11-12 · 范围：`lovia/workspace`（新增 `remote/`，几处协议清理）、core（`tools/base.py`、`runtime/tool_calls.py`：权限判定可 await）、`lovia/web`（`workspaces`、`api/`、`store`、`attachments`、`supervisor`、前端）
>
> 行号指向 `main`（`5965346`，v0.9.166）。

## 结论

主要环境仍是本机；远程是让一个对话的工作区落在**已有的 Docker 容器**或 **SSH 机器**上，在同一个界面里和本地项目混用。

做法是一句话：**会话引擎跑在文件所在的那一侧，本地只留一个转发代理。** 远端跑的是原样的 `LocalWorkspaceSession`，由本地在连接时把源码经管道送过去，远端只需要 `python3` ≥ 3.10，不安装任何东西。

它拆成两个互相独立的决定：

1. **web 只认协议，不认 `LocalWorkspace`。** 不含任何远程代码，本身就让任意第三方后端能接入。
2. **远程后端**是接缝后面的一个模块（`lovia/workspace/remote/`）。它错了，重写的是这一个模块。

本机路径的行为和性能不变，这是硬约束。

## 现状

**核心已经是协议驱动的。** `RunLoop` 和内置工具只依赖 `WorkspaceSession` / `WorkspaceLike`（`workspace/protocol.py`）。

**web 写死了本地。** 非 `LocalWorkspace` 的后端在 web 里能跑但残废：

| 位置 | 写死的东西 |
| --- | --- |
| `web/workspaces.py:229` | `bind()` 只接管 `LocalWorkspace`；其他后端每轮重开会话，后台进程活不过一轮 |
| `web/api/workspace.py:96` | Files 面板直接构造 `LocalWorkspaceSession`；`raw` 用 `FileResponse`；上传直接写本地盘 |
| `web/api/workspaces.py:61` | 目录浏览用 `os.scandir` |
| `web/api/deps.py:280` | `admit_root` 用本地 `is_dir()` / `resolve()` |
| `web/store.py:442` | `grant_context` 自己调本地 `resolve()` |
| `web/attachments.py:39`、`web/supervisor.py:107`、`web/app.py:334` | `isinstance(..., LocalWorkspace)` |
| `workspace/workspace.py:549` | `WorkspaceSessionBinding.workspace` 的类型就是 `LocalWorkspace` |

**三处同步契约在远程下不成立：**

| 契约 | 现状 | 处理 |
| --- | --- | --- |
| `decide_path` / `decide_command` | 同步，靠本地 `Path.resolve()` 跟符号链接（`local.py:311`）；`Tool.decide` 同步（`tools/base.py:153`） | 允许返回 awaitable（§3） |
| `background_processes()` | 同步，被提醒注入器和 web 进程列表调用（`tools.py:671`、`api/workspace.py:457`） | 代理维护快照，保持同步（§5） |
| `instructions()` | 在本地解析 root、看 `.git`、读包管理器配置（`workspace.py:197`） | root 事实由会话提供（§2） |

**一个潜在缺陷：** 前台命令没指定 stdin（`local.py:1043`），继承宿主进程的输入；后台那条是 `DEVNULL`。远端服务把 stdin 当协议通道时，读 stdin 的命令会抢走协议数据。单独修，本地也受益。

## 为什么是"引擎在远端"

策略判定嵌在遍历内部，逐条目执行：`_walk_files` 用 `_read_hidden` 决定是否进入每个子目录（`local.py:973`），`_list_children`、`_list_matching`、`grep` 对每个条目先解析符号链接再判一次（`local.py:780`）。

所以**策略必须和文件在同一侧**。策略留在本地的任何拆法，远程遍历都要么每个条目一次往返，要么把策略求值器送过去，而后者就是本方案。能搬的最小单位是整个会话引擎。

## 设计

### 1. 结构

```
web ──①后端接缝──▶ WorkspaceLike（本地 / 远程 / 第三方）
                     └─ RemoteWorkspace ──▶ RemoteHost（一条连接，多个会话）
                                              └─②传输接缝──▶ ssh | docker exec | 任意能给出 stdio 的命令
                                                   └─ 远端：引导 → 服务循环 → LocalWorkspaceSession
```

- **连接和会话分开。** 一个 `RemoteHost` 持有一条懒打开的连接，上面可以开多个会话（对话的会话、Files 面板的只读会话）。否则 Files 面板每个请求都要重新握手。
- 关闭一个会话只杀它自己的后台进程；连接断开杀掉全部。

### 2. 库 API

```python
from lovia.workspace import RemoteHost, Workspace

box = RemoteHost.ssh("box")                       # 读 ~/.ssh/config
api = RemoteHost.docker("my-api", user="dev")     # 已在运行的容器
any_ = RemoteHost("k8s", command=["kubectl", "exec", "-i", "pod", "--"])

agent = Agent(workspace=Workspace.remote(box, "/srv/project", mode="coding"))
...
await box.aclose()
```

- `Workspace.remote(host, root, ...)` 返回 frozen 的 `RemoteWorkspace`，接受和 `Workspace.local` 相同的策略参数，除了 `executor` 和 `command_decider`（见"不做"）。
- `RemoteHost` 的参数：`name`（后端身份，进授权上下文）、`python="python3"`、以及传输。传输是一个极小的协议：给定远端要执行的 argv，返回一条字节管道。`ssh` / `docker` 两个构造器负责各自的引号规则（ssh 会把参数拼成一行交给远端 shell，`docker exec` 不会）。
- **工具集、注入器、提示词从策略推导的逻辑抽成共享函数**，`LocalWorkspace` 和 `RemoteWorkspace` 都调用它，不靠继承。
- **`instructions()` 不改签名。** 它依赖文件系统的三件事（root 名、是否 git 仓库、Python 包管理器）由会话打开时取回，缓存在 `RemoteHost` 上；`RemoteWorkspace.instructions()` 同步读这份缓存。工作区先于系统提示词打开（`runtime/loop.py:725`、`:1453`），运行时一定读得到；没打开过就调用，退化成不含这几句的通用措辞。web 里一个对话持有一个会话，事实在会话期间不变，提示词前缀比本地每轮重新探测更稳定。
- `RemoteHost.ssh` 固定带 `BatchMode=yes`：stdin 是协议通道，密码或主机指纹的交互提示会让连接挂住，开了之后立刻失败并报错。远程主机必须配好密钥或 agent 认证。

### 3. core 和 workspace 层的改动

**core（`workspace/`、`web/` 之外）只有三项，都与远程无关，core 里没有一行知道远程的存在：**

| 改动 | 规模 | 原因 |
| --- | --- | --- |
| 权限谓词可以返回 awaitable：`Tool.decide` 的返回值由运行时判断后再 await（`runtime/tool_calls.py:227`、`plugins/mcp.py:700`） | 两个调用点 | 需要 I/O 才能判定的权限都用得上。写法与 `ViewInjector`（"may be sync or async"，`plugins/base.py:55`）一致；同步谓词和直接调用 `decide` 的代码不受影响，不是破坏性变更 |
| `clip_text` 从 `tools/base.py` 挪进 `workspace/local.py`（实现时发现它只有这一个使用方，也不是公开导出，所以不另设叶子模块，原路径不保留） | 挪一个函数 | `local.py` 引用它，而 `tools/base.py` 会拉进整个核心 |
| `approvals.py`、`exceptions.py` 只导入标准库 | 零行，现状如此 | 见下面"两个代价" |

**workspace 层：**

| 改动 | 原因 |
| --- | --- |
| `WorkspaceSession.decide_path` / `decide_command` 可返回 awaitable；本地实现仍同步返回 | 远程判定要解析远端符号链接；本机零开销 |
| 远程判定带超时，超时按 `ask` 处理（与 `_UNJUDGED` 一致，`tools.py:72`） | 现有工具超时覆盖不到判定阶段 |
| 引擎文件只导入标准库：`local`、`policy`、`paths`、`command_guard`、`rules`、`errors`、`types` | 它们会被送到远端执行 |
| `types.py` 的 10 个结果类型从 pydantic 改为 frozen dataclass | 上一条的唯一实质障碍；破坏性变更，1.0 前做完 |
| `WorkspaceSessionBinding` 接受任意 `WorkspaceLike`；`set_policy`、`write_bytes`（含独占创建）进 `WorkspaceSession` 协议 | web 不再探测具体类型 |
| 有副作用的内置工具（`write_file`、`edit_file`、`shell`、`kill_process`）声明 `retries=0` | `run_tool` 会重试一般异常（`tools/base.py:321`）；回包丢失时重试等于重复执行 |
| 前台命令 `stdin=DEVNULL` | 见"现状" |
| 新增 `workspace/remote/` | 独立子包，按需导入，不被任何现有模块引用 |

纯策略计算（`WorkspacePolicy`）保持同步，不动。

**两个代价，都是为远端零安装付的：**

1. **一条从下游伸到上游的约束。** `approvals.py` 和 `exceptions.py` 被要求保持纯标准库，原因是 workspace 层的引擎要被送走。代码上没有反向依赖；这两个文件的性质（一个异常层次、几个判定用的 dataclass）本来就不该有第三方依赖，规则只是把现状写明，并由测试而不是约定守护。
2. **偏离"数据模型用 pydantic"的约定。** 这 10 个类型只做构造和属性访问，用不到校验，改成 dataclass 不违反设计哲学，但会和其他结果模型不一致。`types.py` 文首要有一句注释说明：它们要在没有 pydantic 的地方运行。

不接受这两项的退路只有远端 `pip install lovia`（见"不做"）。

**守护测试**：在 `python -I -S` 的子进程里（没有 site-packages）经引导路径跑一遍会话行为测试。只测 import 不够，它测不到延迟导入。这条写进 AGENTS.md。

### 4. 引导

1. 本地执行 `<传输> python3 -u -c <引导代码>`。引导代码十几行，只做一件事：从 stdin 读一个带长度前缀的源码包，注册内存导入器，启动服务循环。
2. 源码包由本地从自己安装的 lovia 里现取，清单固定为 §3 列出的文件加服务循环本身，放在一个 `__init__` 为空的合成包里。不能直接 `import lovia`，它会加载 httpx 和 yaml。
3. 服务循环启动时把协议用的 fd 挪走，fd 0 指向 `/dev/null`，fd 1 指向 stderr。

两端代码永远同源，不存在版本错配。远端不落盘。

握手前先探测 `python3` 及版本，不满足就报错，hint 写明要求和检测到的版本。**远端有合适的 Python 由用户保证**，不做任何自动补救。

### 5. 协议

采用 JSON-RPC 2.0 的约定，按行分帧。方法名就是 `WorkspaceSession` 的方法名，再加少量连接级方法。

| 方面 | 规定 |
| --- | --- |
| 握手 | 协议号、Python 版本、平台、home、服务进程的启动目录（容器是 `WORKDIR`，ssh 是 home，用作目录选择的起点）；之后 `open(root, policy, env, limits, …)` 返回会话 id、规范化后的 root 和 root 事实 |
| 策略 | 按数据编码（规则、默认值、baseline）；`set_policy` 是一次调用 |
| 二进制 | base64；`read_bytes` 的上限在远端生效；单帧上限从 `limits.max_file_read_bytes` 推导，不单独配置 |
| 列表结果 | `{items, truncated}`，对应 `ClippedList` |
| 错误 | `{code, message, data: {type, hint}}`，映射回同名异常；未知类型落到 `WorkspaceError` |
| 取消 | 取消通知带请求 id，只取消那一个请求；`run()` 被取消时本来就会杀掉自己的进程组 |
| 关闭会话 | 独立的方法。停止一轮不会杀掉跨轮的后台进程 |
| 后台进程状态 | 进程集合或状态变化时，服务端推送一条通知；代理保存最近一次快照，`background_processes()` 同步读它 |
| 存活 | 双向心跳；服务端读到 EOF 或心跳超时，关闭全部会话并自行退出 |
| 连接级方法 | `resolve_root(path)`、`list_dirs(path)`：给 web 的目录选择用，不属于任何会话 |

**断线语义**：所有在途请求以 `WorkspaceDisconnectedError` 失败，消息说明结果未知。不透明重连，不重试。下一轮运行重新建立连接和会话，后台进程不恢复，与"进程随会话消亡"一致。

### 6. web

**会话绑定。** `WorkspaceSessions.bind` 改为按能力判断：后端能 `bind(session)` 就接管；能 `set_policy` 就在只换模式时保留会话。远程对话的后台进程由此跨轮存活。

**远程位置是对话级数据，不是配置。** 它对应的是"对话在哪个目录"，沿用 `web-workspace-switching.md` 的三层划分，不新增配置段，也不新增命令行参数：

| 现有概念 | 存放 | 远程的对应 |
| --- | --- | --- |
| 对话的目录 | `chat_sessions.workspace_root` | 加一列 host（`NULL` 表示本机） |
| 已知的项目列表 | `chat_workspaces`，在侧栏里添加 | 远程项目也是一行，带 host |
| 能开哪些目录 | 部署约束表（该文 §2） | 加一列"能否连远程" |

**主机没有注册表，身份就是连接方式。** 打开文件夹时选位置类型：本机、SSH、容器。SSH 填 `~/.ssh/config` 里的主机别名（用户名、端口、密钥都在 ssh 自己的配置里，lovia 不重复一份）；容器填容器名和可选的用户。存成 host `ssh:box`、`docker:my-api` 加 root。`ChatBinding` 增加 `host`；root 由后端规范化；`grant_context` 带上 host，两台机器的同名目录不共用授权。展示为 `box:/srv/project`。对话创建时选定，之后不变，和现在的目录一样。

**界面只接受结构化的形式，不接受原始命令。** 别名和容器名严格校验（不以 `-` 开头、不含空白），界面上拼不出任意命令。界面添加的主机解释器固定为 `python3`。原始命令的传输和自定义解释器只走库 API。

**部署约束：**

| 启动方式 | 能否连远程 |
| --- | --- |
| 回环地址（默认） | 能 |
| 回环 + `--lock-workspace` | 不能 |
| 非回环地址，含 `--allow-workspace-switch` | 不能 |
| `--no-workspace` | 不能 |

回环地址上本来就能开任意目录、用 trusted 模式，智能体的 shell 已经可以直接执行 `ssh box`，远程没有增加权限。`--allow-workspace-switch` 放开的是服务器上的目录；远程是用服务器的密钥去碰别的机器，不是一回事，所以不随它开放。规则在服务端执行。

**库 API 与 `roots` 对称：**

```python
WorkspaceSwitch(roots="any", remotes="any")              # lovia web 在回环地址上的行为
WorkspaceSwitch(remotes={"box": RemoteHost.ssh("box")})  # 部署者显式声明，只有这些
WorkspaceSwitch()                                        # remotes=None：没有远程（默认）
```

要在非回环部署里用远程，走 `--app` 自己声明。声明过的主机上任意目录都可以打开：边界是部署者选的那个账号能访问什么，已经在操作系统层面生效。

**Files 面板。** 只走协议方法。只读视图是同一条连接上的另一个会话（readonly 策略，沿用 `_view_session` 的规则：只带 `denied_paths`）。"最近文件"仍是一次 `list_files(pattern="**/*")`。`raw` 用 `read_bytes`，上限沿用 `limits.max_file_read_bytes`（本地 `raw` 已经用它，`api/workspace.py:383`），远程首版不支持 Range。二进制嗅探改走 `read_bytes(max_bytes=…)`。

**上传和附件。** 上传走 `write_bytes` 的独占创建，重名处理留在 web。附件解析改走会话读取。

**目录选择和 `admit_root`。** 远程走连接级的 `resolve_root` / `list_dirs`，从握手报告的启动目录开始。

**断线。** 运行以明确的错误结束；进程面板显示连接已断开。

### 7. 实现与本文的出入（PR 6）

- **后台进程状态不走推送。** §5 写的是"服务端推送通知，代理保存快照"。实现时改为和判定一样：`background_processes()` 可以返回 awaitable，每次现问远端（注入器本来就允许异步，web 的进程列表在异步端点里）。这样协议里没有服务端主动消息，也没有快照过期的问题，代价是每轮多一次往返。
- **传输接缝是 `command` + `quote`，不是一个 Protocol。** `RemoteHost(name, command=(...))` 给出"带着标准输入输出在远端执行后面参数"的命令前缀，`quote=True` 表示它会把参数交给远端 shell。只有 SDK、没有命令行的沙箱暂时接不进来；连接层内部已经只依赖一对读写流，需要时再抽协议。
- **握手是远端先说话。** 远端启动后发一行 `{"ready": 协议号, python, platform, pid, cwd, home}`，本地跳过此前的任何输出（登录 shell 的启动文件可能往 stdout 打印东西）直到读到它。
- **`resolve_root` / `list_dirs` 没做**，它们只有 web 的目录选择会用，留给 PR 7。
- **`Workspace.docker()` 占位已删除**，`RemoteHost.docker()` 取代了它指向的方向。
- **默认 baseline 不随策略发送**：它按平台构建，留给远端按自己的平台解析。预设里按平台拼装的命令规则包仍然随策略发送，本机是 Windows 而远端是 Linux 时会不匹配，这一点没有处理。
- 源码包在运行时从已安装的包里读 `.py` 文件，只装了 `.pyc` 的环境用不了远程工作区。

### 8. 实现与本文的出入（PR 7a）

- **远程位置不加数据库列，编码进现有的 `workspace_root` 字符串。** §6 写的是给 `ChatBinding` 加 `host`、给两张表各加一列。实现前清点了一遍：`workspace_root` 贯穿对话表、定时任务表、侧栏分组的 SQL（`PARTITION BY workspace_root`）、事件、接口、子任务和调度器，并行再穿一个字段要改三十多处。改为一个字符串键：本地目录还是它的解析后路径，远程位置写成 URL 的样子——`ssh://box/srv/project`、`docker://dev@api/app`（`remotes="any"`），`remote://名字/路径`（声明过的主机）。只有打开、校验、描述工作区的几处用 `WorkspaceRef.parse` 解释它，其余代码只是传递这个字符串。没有表结构迁移，接口形状不变。
- **每次运行不到远端确认目录是否存在。** 本地的 `admit_root` 每轮都检查目录还在；远程每轮检查意味着每轮多一次往返。远程位置每轮只按 `remotes` 做许可检查，目录不在由会话打开时的错误体现。加入列表时（`POST /api/workspaces`）才到远端解析并确认，存的是解析后的路径，所以同一个目录只有一项。
- **远程对话的策略取自本地同模式的工作区**（`build` 钩子的结果），在远端执行；`env` 不带过去（那是为本机写的）。
- **新增 `agent_for_remote(agent, ref)` 钩子。** `agent_for` 拿到的是本机目录，远程对话不调用它。但 `lovia web` 提供的 Agent 是按启动目录构建的，已经带着那个目录的 `AGENTS.md` 和技能；只是"不调用"会让远程项目沿用本机项目的指令。所以需要一个对应的钩子把它们去掉，`lovia web` 用它只保留全局提示词和不依赖项目的技能目录。
- **在远程对话里派生的子任务**：自克隆的子任务带着运行时那个远程工作区，而对话的位置和模式是从 Agent 自己的本地工作区推出来的。派生时把本地工作区换回去，任务运行时再按绑定重建远程工作区。
- **带附件的消息在入口处拒绝**（404 `feature_unavailable`），不是丢掉附件只回答文字。
- **`remotes="any"` 时主机由 web 按需构建并在关闭时回收**，每个目的地一个，名字是 `ssh:box` / `docker:api`，授权上下文用的也是它。声明的主机归声明者关闭。
- **新增错误码 `workspace_unreachable`（502）**：加入列表时连不上主机。
- 目录补全（`GET /api/workspaces/dirs`）仍只支持本地，留给 7b。Files、上传、附件对远程对话返回 404 `feature_unavailable`，留给 7c。

## 注意事项

- **信任边界。** 远端命令以远端用户身份运行；策略仍是诚实的范围限定，不是隔离。凭据（ssh 密钥、Docker 访问）在 web 服务器上；能访问 Docker 等同宿主机 root。多用户部署（#221）下谁能用哪台主机要另行设计。
- **容器。** 杀掉 `docker exec` 客户端不保证杀掉容器里的进程，收尾只能靠服务端读到 EOF 后自清理加心跳超时。默认用户常是 root，写出的文件属主会不对，所以 `RemoteHost.docker` 有 `user`。容器重启等于断线。
- **半开连接。** ssh 建议配 `ServerAliveInterval`；心跳是兜底。
- **`ask` 仍在 web 端弹。** 远端只执行 `deny`，和本地会话一致（`local.py:353`）。
- **延迟。** 每次工具调用多一次往返；遍历和搜索在远端完成，不随条目数增加往返。

## 不做

| 不做 | 原因 |
| --- | --- |
| 预装服务端 | 两种部署方式是两套测试矩阵，还带来版本错配 |
| 缺 Python 时送运行时；降低引擎的 Python 下限 | 由用户保证 `python3` ≥ 3.10 |
| 把 `local.py` 拆成"门 + I/O 驱动" | 见"为什么是引擎在远端" |
| 每次操作单独执行一次、无常驻服务 | 后台进程和写锁都在进程内存里 |
| sshfs 挂载 + `executor=` | web 服务器要装 FUSE；`executor` 拒绝后台进程（`local.py:1147`） |
| Docker 沙箱（本机项目挂进容器） | 不是这次的需求 |
| 远程下的 `command_decider`、自定义 `executor` | 是 Python 回调，送不过去；配置了就报错并给 hint，不静默丢弃 |
| 远程项目的 `agent_for`、`AGENTS.md`、skills 文件 | 现有实现是本地同步读；首版远程对话不加载，文档写明 |
| 断线后恢复后台进程；透明重连 | 结果未知的操作不能自动重放 |
| 端口转发 | 容器的端口由容器自己的配置发布；SSH 机器上的 dev server 暂不可在浏览器里直接打开 |
| Windows 远端 | 路径规则只在 CI 上验证过 |
| `raw` 的 Range / 流式 | 有需求再加 |
| `config.json` 的 `remotes` 段、`--remote` 参数 | 远程位置是对话级数据（§6）；不能在设置页改的 `config.json` 段不符合现有约定，结构化数据也不适合做命令行参数 |
| 远程主机上的目录白名单 | 边界是远端账号的权限；以后要加是给 `remotes` 的值加字段，不破坏已有用法 |
| 非回环部署下从界面连远程 | 用服务器的密钥碰别的机器；需要的部署走 `--app` 声明 |
| 界面上指定解释器路径 | 首版固定 `python3`；有人撞上再给"打开"对话框加高级字段 |
| 从 `~/.ssh/config` 或 `docker ps` 列出候选 | 首版是文本输入 |

## 原型（进 PR 4 之前）

一次性代码，放 `tmp/`，不进主干：引导 + 服务循环 + 读、写、执行、取消四个操作，分别用本地子进程、一个真实容器、一台真实 SSH 机器跑通。要回答三个问题：

1. 引导在三种传输上是否稳定（引号、缓冲、stderr）。
2. 取消和断线之后，远端进程是否清干净，尤其是 `docker exec`。
3. 单次操作的延迟。

结果不理想时，本文的 §4、§5 重写，其余不变。

### 结果（2026-11-11）

代码在 `tmp/remote-spike/`（`spike.py` 客户端和用例，`far.py` 服务循环，`pydantic_shim.py` 仅原型用）。远端跑的是未改动的引擎文件。

四种传输上 15 项检查全部通过：本地子进程（`python -I -S`，无 site-packages）、"shell + 管道中继"（模拟一个可以被杀掉的传输进程）、真实 SSH（一台公网 Linux 主机，Python 3.12，远端登录 shell 是 zsh）、经 SSH 进容器（同一主机上的 `python:3.12-slim`，`ssh … docker exec -i`）。

| | 本地 | SSH | SSH + 容器 |
| --- | --- | --- | --- |
| 启动到就绪 | 约 45 ms | 366 ms | 1418 ms |
| 协议往返（ping） | 0.1 ms | 12 ms | 13 ms |
| 读小文件 | 0.3 ms | 13 ms | 14 ms |
| `run('true')` | 4 ms | 14 ms | 16 ms |
| 1 MB 二进制写入再读回 | 15 ms | 2.4 s | 2.0 s |

| 问题 | 答案 |
| --- | --- |
| 引导是否稳定 | 是。源码包 43 KiB；引导代码经远端 shell（含 zsh）引号传递无误；远端 stderr 无输出 |
| 取消和断线后是否清干净 | 是，四种传输都是。取消只杀那一个命令，会话照常可用；关闭 stdin 或杀掉传输进程后，服务进程、后台进程、前台命令全部退出 |
| 单次操作延迟 | 协议和引擎的开销可以忽略，延迟就是一次网络往返。连接建立是一次性的，证实了"连接和会话分开"（§1）的必要 |

**`docker exec` 的专项结论**：在 Docker 主机上直接 SIGKILL 掉 `docker exec -i` 客户端（中间没有 SSH），容器里的进程约 1 秒内读到 stdin 的 EOF（Docker 29.4）。所以"靠 EOF 自清理"对容器成立；心跳超时仍然保留，兜住半开的 TCP 连接。

其他确认的点：引擎除 `pydantic` 和 `clip_text` 外确实只依赖标准库；读 stdin 的命令抢不到协议数据；命令执行期间服务仍能应答。

**没有覆盖的**：本机直接执行 `docker exec`（开发机没有 Docker，只测了经 SSH 的路径和主机上的专项实验）；网络中途断开而两端进程都活着的半开连接；Python 3.10 / 3.11；Windows 和 macOS 远端。

**大文件要留意**：1 MB 走了 2 秒，瓶颈是这条链路的带宽加 base64 的 4/3 膨胀。10 MB 的 `raw` 上限在慢链路上会是十几秒，PR 7 设计 Files 时要考虑分块或压缩。

**写进实现的三条教训：**

1. **引导必须用 `os.read` 读 fd 0，不能用带缓冲的 `sys.stdin`**：缓冲读会多吞掉紧跟在源码包后面的协议字节。
2. **传输进程死后，本地要主动关掉自己这一端的管道。** 远端靠读到 EOF 来清理；本地写端不关，远端就永远等不到。原型第一次跑时因此留下了孤儿进程。
3. **不能无限期 `await proc.wait()`。** asyncio 要等所有管道都关闭才返回，远端若还有进程握着 stdout / stderr，它会一直挂住。代理必须带超时，并且不以它作为"已断开"的判据。

## 分期

| PR | 内容 | 含远程代码 |
| --- | --- | --- |
| 1 | 前台命令 `stdin=DEVNULL`；有副作用的内置工具 `retries=0` | 否 |
| 2 | workspace 层：`WorkspaceSessionBinding` 泛化；`set_policy`、`write_bytes` 进协议 | 否 |
| 3 | web：会话绑定只认协议（任意后端的会话按对话持有，后台进程跨轮存活） | 否 |
| — | 原型 | — |
| 4 | 引擎只依赖标准库：结果类型改 dataclass、`clip_text` 挪位、守护测试 | 否 |
| 5 | 权限判定可 await | 否 |
| 6 | `lovia/workspace/remote/`：协议、引导、代理、`RemoteHost`、`Workspace.remote`；工具集和提示词推导抽成共享函数（第二个调用方在这里才出现）；测试全部走本地子进程 | 是 |
| 7a | web 服务端：`WorkspaceSwitch.remotes`、`WorkspaceRef`、每轮按绑定重建远程工作区、加入列表时到远端确认、授权上下文、部署约束（见 §8） | 是 |
| 7b | web 界面：打开文件夹时选本机 / SSH / 容器，远程目录补全，断线状态 | 是 |
| 7c | web：Files、上传、附件改走会话（要给协议加文件元信息和分段读取，见下） | 是 |

**Files、上传、附件为什么不在 PR 3。** 它们现在依赖的东西协议里还没有：`raw` 靠 `FileResponse` 提供 Range、ETag 和流式（本地不能退化成整文件读进内存）；文本预览先读前 1 KB 判断是否二进制；上传绕过策略直接写盘（只读模式的对话也能上传）；附件用本地路径构造 `ImagePart`。要改走会话，协议得先有"文件元信息"和"读一段"之类的方法，而这些方法的形状应该和远程协议一起定，不该在没有第二个后端时猜。

PR 1–3 对本机用户直接有用，也是任何第三方后端的前提。PR 6、7 合入时同步两份 README 和 `docs/{en,zh}/workspace.md`。
