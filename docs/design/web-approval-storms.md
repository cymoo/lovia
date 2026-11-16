# 审批风暴：模式即时生效、子任务审批、引号里的字面量

> 状态：已实现（#457–#460） · 2026-11-16 · 范围：`lovia/web`、`lovia/workspace`（session engine）、`lovia/plugins/subagents.py`。
>
> 行号指向 `main`（`aab66300`，v0.9.215）。

## 背景

`lovia web` 的两条用户反馈：

1. 主 Agent 是完全权限，subagent 仍是"工作区内修改"。用户不大会去子任务里点审批。
2. 切到完全权限后仍然弹审批，要等本次回复结束才生效。

两条的根因相同：**权限模式在 run 开始时定死**。排查中还发现几处会让审批成串出现的地方（第 4 节）。

## 结论

| # | PR | 改动范围 |
| --- | --- | --- |
| 1 | 模式切换即时生效，子任务跟随父对话 | `web/workspaces.py`、`web/api/sessions.py`、`workspace-mode.js` |
| 2 | 子任务共用父对话的"始终允许" | `web/supervisor.py`、`web/api/chat.py`、`web/api/sessions.py` |
| 3 | 子任务的审批显示在父对话 | `web/supervisor.py`、`web/schemas.py`、`chat.js`、`sessions.js`、`plugins/subagents.py` 一句提示词 |
| 4 | shell 检查不把引号里的字面量当作展开 | `workspace/policy.py` |

不新增依赖，不新增核心抽象。第 4 项改的是 session engine，只用标准库，`tests/workspace/test_engine_stdlib.py` 必须继续通过。

## DX / UX 复查后加上的

在最初的三项修复之外，从使用者和开发者的角度又补了这些，都放在上表对应的 PR 里：

- **UX**
  - 切换模式的提示说清楚何时生效："立即生效"；只有从只读放宽时说"从下一条回复起"（第 1 项）。
  - "需审批"的点随状态及时更新，不再等下一次刷新（第 3 项的 `run_status`）。
  - 父对话里的子任务审批卡写明是哪个任务，并能一键打开它（第 3 项）。
  - 任务对话的授权面板显示实际生效的授权，即父对话的（第 2 项）。
- **DX**
  - `set_policy` 的 docstring 写明可以在 run 中途调用（第 1 项）。
  - 核心 Subagents 的提示词不再对 web 部署说错话（第 3 项）。
  - `RunInfo.pending_approval` 和 `run_status` 让部署方自己的 UI 也能显示子任务审批，不必订阅每个任务的流（第 3 项）。

## 不做和暂缓

| 项 | 决定 | 原因 |
| --- | --- | --- |
| 切换模式后重新判定正在等待的那张卡 | 不做 | 要么让核心为 `ApprovalRequired` 加重新判定的回调，要么在 web 层自己找工具和上下文。只差一次点击，用户这时就在页面上 |
| 审批卡上加"切到完全权限"按钮 | 不做 | 模式只有输入框旁边那一个开关。提升权限的按钮放在"允许"旁边，误点的代价太高 |
| 一次回复中审批达到 N 次后提示切换模式 | 不做 | 阈值没有依据；第 1–4 项落地后再看是否还需要 |
| 子任务单独的模式开关 | 不做 | 子任务替父对话干活，权限跟父对话走。任务对话里仍能看到模式，切换只影响它自己 |
| 审批超时后模型换写法重试 | 不改 | 超时原因已经告诉模型"用户在 N 分钟内没有回答"（#352）。第 3 项让卡片能被看见之后，超时本身会变少 |
| 收窄 `-o` 输出选项启发式（`grep -o`、`ps -o`） | 暂缓 | 有意保守（#347 第 5 轮）。不会让审批成串出现，只是没有"始终允许" |
| trusted 下写 `.git/`、`.github/workflows/`、`.vscode/`、`.lovia/` 不可授权 | 不改 | 有意的保护（`PROTECTED_CONFIG`）；改这些文件的频率很低 |
| 页面在后台时提示有待审批（#351） | 暂缓 | 已有 issue；与本设计无关 |

## 1. 模式切换即时生效，子任务跟随父对话

**现状。**

- `PATCH /api/sessions/{id}` 只写库（`sessions.py:401`），前端提示"正在进行的回复仍按原模式完成"（`workspace-mode.js:181`）。
- 每个对话有一个跨 run 存活的工作区会话（`WorkspaceSessions`，`workspaces.py:432`）。下一次 run 开始时 `bind()` 发现只有策略不同，就调用会话的 `set_policy`（`workspaces.py:470`），保住 dev server。
- 工具调用的判定读的是会话当前的策略：文件和 shell 工具经由 `ctx.workspace`（就是这个会话），MCP 工具读 `ctx.workspace.policy.shell_default`（`web/mcp.py:208`）。所以**中途换策略，后续调用立刻按新模式判定**，引擎不用改。
- 子任务继承派生它的那次 run 的快照（`subagents.py:197`，`deps.inherited`）。父对话中途切换后，这次 run 派生的子任务、已经在跑的子任务都还是旧模式。

**改法。**

1. `WorkspaceSessions.retune(session_id, cfg) -> bool`：只做 `bind()` 里"只有策略不同"那一支，即 `set_policy` 并更新持有的配置；配置有别的差异时返回 False，**绝不关闭会话**（run 正在用它）。`bind()` 改为调用它。
2. PATCH 切换模式时，在该对话的 `_start_lock` 内：
   - 写库；
   - 如果有正在进行的 run：按新的 binding 重建工作区配置（`effective_agent(ctrl.agent, binding, hooks=False)`），交给 `retune`；成功后把 `ctrl.binding` 换成新的 binding，之后派生的子任务就继承新模式。
   
   拿锁是为了不和正在开始的 run 交错：开始前读到旧模式、开始后才被改的情况不会出现。
3. 级联：`RouterDeps.switch_mode` 写库后，对任务树下的每一层子任务做同样的事：写库、发 `session_workspace_changed`，有活的 run 就 `retune`。`run_child` 在任务行写入之后、任务开始之前按同一规则（`deps.inherited`）再读一次父对话的模式，免得任务建立期间发生的切换漏掉它。必须在开始之前读：开始之后，已经在等审批的那次调用不会再判定。
4. 前端提示：有回复正在进行时改为"已切换为 X，正在进行的回复立即生效"。只有这次回复是从"只读"开始（按 run id 记下开始时的模式）时，才提示"文件编辑和命令从下一条回复起可用"（见下面边界的第一条）。

**边界。**

- **工具集在 run 开始时确定。** 只读没有写文件和 shell 工具，所以从只读放宽时，本次回复里策略变了，但模型手里没有这些工具。收窄到只读是即时的：写操作和 shell 会在会话层被拒，这是期望的行为。
- **系统提示词里描述模式的那段不变。** 本次回复内模型可能仍以为某些操作需要审批。好处是前缀字节不变，缓存不失效。
- **已经在等待的那张卡**不重新判定（见"不做"）。
- **handoff 目标**每次 run 各自打开会话（`loop.py:1608`），不经过 `WorkspaceSessions`，因此不跟随。`lovia web` 默认没有 handoff，写进 docstring。
- **自定义 `WorkspaceSwitch.build`** 如果按模式返回不止策略不同的配置，`retune` 返回 False，这次 run 保持原模式，记一条日志；下次 run 照常生效。
- `LocalWorkspaceSession.set_policy` 的 docstring 改掉"在两次 run 之间调用"的说法：任何时候都可以调用，已经通过检查的操作不再重新判定。

**测试。** 用 `ScriptedProvider`：一次 run 先调用一个在 coding 下会询问的 shell 命令，审批前把对话切到 trusted；下一个同样的调用不再询问。收窄的情况：trusted 切到只读后写文件被拒。级联：活的子任务切换后不再询问；`retune` 遇到非策略差异返回 False，会话不被关闭。

## 2. 子任务共用父对话的"始终允许"

**现状。** 授权按 `(session_id, scope, context)` 存（`store.py:1159`），`context` 是"agent 的注册名 @ 工作区根目录"（`supervisor.py:103`）。子任务是另一个 session，agent 名也不同（子任务 agent 没注册，`name_of` 退回 `agent.name`）。父对话里点过"始终允许 npm"，每个子任务还要再问一遍；三个并发子任务就问三遍。

**改法。** 只有子任务有 `parent_id`（`subagents.py:272`），以它为准：

- `RouterDeps.grant_owner(session_id) -> tuple[str, str | None]`：沿 `parent_id` 走到任务树最上层的对话，返回它和它的 run 所用的 agent 注册名（`agent_of`；没存 agent 的旧对话对应默认 agent）。
- `RunController` 开始时记下 owner。`_granted` 和批准时写授权（`chat.py:395`）都用 owner 的 session id；`grant_context()` 对入口 agent 用 owner 的 agent 名，handoff 目标仍用它自己的名字。
- `GET/DELETE /api/sessions/{id}/grants` 对子任务作用于父对话的授权。任务对话的授权面板列出的就是父对话的。
- 卡片文案"在此对话中始终允许"不变：子任务属于这个对话。

**取舍。** 子任务与父对话在同一个目录、替同一个用户干活。共用授权正是用户点"始终允许"时的预期。

**测试。** 父对话的授权让子任务的同一 scope 直接放行；在子任务卡上点"始终允许"，写到父对话上；兄弟子任务也直接放行。

## 3. 子任务的审批显示在父对话

**现状。** 子任务的审批卡只在它自己的任务对话里。父对话只有顶栏 Tasks 按钮上一个"需审批"的点（`sessions.js:509`），而且这个点不会自己更新：状态从 running 变成 `blocked_on_approval` 时没有全局事件，`/api/events` 只有 run 的开始和结束，要等下次刷新 `/api/runs` 才看得到。主 Agent 在 `wait_subagents` 里等，子任务最多挂 600 秒（`ANSWER_TIMEOUT`）后被拒。用户看到的是"卡住了"。

另外，核心 Subagents 的提示词告诉模型"子任务里需要审批的工具会被自动拒绝"（`plugins/subagents.py:243`）。核心默认模式下确实如此，但在 web 里子任务会等用户回答。

**改法。**

1. **生命周期事件 `run_status`** `{session_id, run_id, status}`：supervisor 在 `status` 变化时发出（`supervisor.py:932` 和 `:936` 两处）。
2. **`RunInfo.pending_approval`**：正在等待的审批，与 `approval_required` 帧同样的结构（`_approval_data`）；没有就是 null。断线重连时客户端照常取 `/api/runs` 快照，事件流不需要回放。
3. **客户端**：收到 `run_status` 只重取 `/api/runs`，刷新 `runsBySession`，任务的"需审批"点也随之及时更新。
4. **父对话的审批托盘**：打开的对话有子任务处于 `blocked_on_approval` 时，在输入框上方为每个子任务显示一张审批卡。
   - 卡头写"任务 t1 · <标题>"，带"打开任务"链接。
   - 卡片主体与对话里的审批卡相同。把 `appendApproval`（`chat.js:832`）里构建卡片的部分抽成 `buildApprovalCard(call, sessionId, onDecided)`，对话和托盘共用。
   - 批准或拒绝时提交的 `session_id` 是子任务的。"始终允许"按第 2 项写到父对话上。
   - 子任务状态回到 running，或者 run 结束，卡片就移除。另一个标签页先作了决定也一样。
5. **提示词**：改成两种模式下都成立的说法，比如"子任务可能无法使用需要审批的工具，只委派它的工具能独立完成的工作"。

**为什么不在父对话里订阅每个子任务的 SSE。** 那样要为每个活的子任务各维持一条连接，还要处理它们各自的回放。一个全局事件加一个快照字段，复用已有的"快照 + 事件"模型（`events.py:13`）。

**测试。** 后端：状态变化时发出 `run_status`；`/api/runs` 带出 `pending_approval`；用子任务的 session id 批准，它会继续运行。前端：哪些请求该显示由纯函数 `pendingTaskApprovals`（`approvals.js`）决定，用 `node --test` 覆盖；用 verify skill 的 stub 模型手动走一遍：父对话派生子任务，子任务请求审批，在父对话里批准。

**未覆盖。** 子任务再派生的子任务（只有自定义 agent 会这样）不会出现在顶层对话的托盘里。

## 4. shell 检查不把引号里的字面量当作展开

**现状。** `_uncertain_word`（`policy.py:319`）只要在任何一个词里看到 `*?[{$`` ` `` 就认为"shell 会展开它"：规则给出的 allow 降为 ask，并且不提供"始终允许"。但检查时引号已经被 shlex 去掉了（`_words`，`policy.py:341`），单引号里的内容也被算进去。实测 coding 下这些命令每次都询问、且不能授权：

```text
find . -name '*.py'          rg 'foo.*bar'            rg -g '*.ts' foo
ps aux | awk '{print $2}'    jq '.[] | .name' a.json  python -c 'print(1*2)'
```

模型最常写的命令里很多都是这种形式，这是 coding 模式下审批成串出现的主要来源。

**改法。** POSIX shell 不展开单引号里的任何字符，双引号里只展开 `$` 和反引号，反斜杠转义的字符也不展开。新增 `_mask_literal(command)`：把这些位置上的展开字符替换为 `x`，其余字符原样保留。被替换的字符都不是空白、引号或 shlex 的标点，所以对替换后的串分词，得到的词与原串一一对应。

几个要点：

- **整行屏蔽，再分段。** 屏蔽对整条命令做：分段是词法的，会在引号里断开（`rg 'a;b' $(id)`），单独屏蔽一段会把多出来的引号当成开引号。屏蔽保持每个字符的位置，也不动分段用的字符，所以两边分出的段一一对应。
- **只读普通引号。** 命令里只要出现会改变引号含义的结构，就整行不屏蔽，按原样判断：here-document（`<<`）、注释（`#`）、嵌套上下文（`$(`、反引号、`${`）。这是一张封闭的清单，用来代替逐个补 shell 语法：外部审查连续三轮找到的拼法（注释里的引号、here-document 正文、转义空格后的 `#`）都属于这一类。代价是少数带 `#` 或 heredoc 的命令拿不到授权，和本系列之前一样。

`_decide_segment` 对词作两种读法：

- **按字面拼写**（不变）：任何展开字符仍让 allow 规则降为 ask。规则是事先为字面参数担保的，而程序可能自己去匹配引号里的 `'*'`（git 的 pathspec）。`test_no_allowed_command_reads_through_an_expansion['*']` 这条既有的不变量照旧成立。
- **按 shell 的读法**（新）：灰名单命令的 ask 保留 `shell:<命令>` 授权，除非某个词确实会被展开。

其余说明：

- Windows（`cmd.exe`）保持现状：单引号不是引号，`%VAR%` 在双引号里也会展开。
- `$'…'`、`"$(…)"`、`"${HOME}"` 仍算展开：`$` 在单引号外或在双引号里。
- 程序自己怎么解释参数（`find -name` 的通配、`bash -c` 的脚本）本来就在规则之外（模块 docstring）。

**已知、未处理。** 分段不识别引号：`jq '.[] | .name'` 会在引号里的 `|` 处断开，卡片上多出一个无意义的 `.name'` 授权（无害，以前什么都不提供）。让分段识别引号是 guard 语法的改动，单独做。

**测试。** 上面这些命令在 coding 下带回 `shell:<命令>` 的 scope；`"$HOME"`、`$(…)`、`` `…` ``、未加引号的部分、`$'…'` 照旧不提供授权；规则允许的命令带上引号里的字面量（`echo '$HOME'`）仍然询问。Windows 的用例由 CI 跑。

## 合并顺序

四个 PR 依次堆叠（每个都升一次补丁版本），从第 1 项开始。第 4 项与前三项没有代码依赖，只是因为版本号放在最后。
