# 运行时切换工作区和权限模式

> 状态：v3 定稿，作为实现基线；剩余边界在各期 PR 的实现评审和测试中处理 · 2026-11-09 · 第一期已合入（0.9.146，#364）；第二期见 0.9.147，其中三处与原文不同：模式越界时降到允许的最宽模式而不是拒绝（§4）；旧对话的关联推迟到第三期（§8）；运行中切换用提示说明，不在输入框里显示两个状态（§4）；第三期见 0.9.148（#368，服务端）和 0.9.149（界面），与原文不同的三处：`WorkspaceSwitch` 多了 `general` 字段承载通用工作区（§5、§6）；未关联对话只在用户发消息时要求先关联，定时任务和子任务报告这类无客户端的运行仍在 Agent 自己的目录里继续（§8）；从列表移除的目录，其对话不在侧栏分组里显示，搜索可以找到（§7） · 对应 [cymoo/lovia#355](https://github.com/cymoo/lovia/issues/355) · 范围：`lovia/web`（`__main__`、`builder`、`config/`、`api/`、`supervisor`、`subagents`、`scheduler`、`workspaces`、`store`、前端）、`lovia/workspace`（一个小改动）
>
> 行号指向 `main`（`612d4d9`，v0.9.145）。

## 结论

改工作区、权限模式这些设置要重启，根子是一个：**工作区挂在 Agent 上，而 Agent 在启动时按 argparse 建一次**（`builder.py:369`）。模型能热切换，是因为 `ConfigRuntime.apply()` 会重建 Agent 再换进 `deps.agents`。但工作区、模式、auto-allow 读的是 `args`，重建出来的还是同一份。

改法是把每个开关放到它该在的那一层：

1. **服务级**（host、port、token、db、日志，以及几个部署约束）：在命令行设置，改了要重启。
2. **设置级**（默认模式、auto-allow、运行上限、follow-ups、memory、subagents、自定义指令、最大输出 tokens）：只放在 `config.json` 里，在设置页修改，下次运行生效。这些开关不再有命令行参数和环境变量。
3. **对话级**（对话在哪个目录、用什么模式）：存在 `chat_sessions` 上。目录在对话创建时选定，之后不再变；模式随时可以切，下次运行生效。

对库的用户来说，开关是一个参数：`create_app(agent)` 的行为和现在完全一样，工作区固定；`create_app(agent, workspaces=WorkspaceSwitch(...))` 才允许在界面上切换。`lovia web` 自己也通过这个参数接入。

## 现状

| 开关 | 现在在哪 | 热加载 |
| --- | --- | --- |
| 模型、搜索、技能目录、角色 | `config.json` → `ConfigRuntime.apply()` | 能 |
| 工作区目录、模式（`--workspace`/`--readonly`/`--trusted`/`--no-workspace`） | `args` → `resolve_workspace()` | 不能 |
| `--auto-allow` | `args` → `resolve_auto_allow()` | 不能 |
| `--max-turns`、`--max-retries`、`--no-followups` | `args` → `create_app()` → `RouterDeps` 字段 | 不能（字段本身可改，但没有代码去改） |
| `--memory-dir`、`--no-memory`、`--no-subagents`、`--instructions*`、`--max-tokens` | `args` → `build_default_agent()` | 不能 |

可以直接沿用的现有机制：

- **运行时解析 Agent。** 每次运行都在消息到来时从 `deps.agents` 取 Agent，正在跑的运行持有的是副本（`config/runtime.py` 文首）。
- **对话级工作区会话。** `WorkspaceSessions.bind()`（`workspaces.py:64`）按对话 id 持有会话。
- **授权记录带根目录。** `grant_context(agent, root)`（`store.py:352`）。
- **权限说明由 policy 生成。** `LocalWorkspace.instructions()`（`workspace.py:190`）按实际的 policy 写系统提示，会话绑定把这件事委托给它（`workspace.py:559`）。所以换了工作区，模型自然知道当前模式，不需要额外告诉它。

## 设计

### 1. 三层开关

| 开关 | 层 | 存放 | 生效时机 |
| --- | --- | --- | --- |
| host、port、root-path、token、db、title、log-level、provider-timeout、trust-env、`--app`、`--check` | 服务 | 命令行（及现有环境变量） | 重启 |
| 启动目录、`--readonly`、`--no-workspace`、`--lock-workspace`、`--allow-workspace-switch` | 服务（部署约束，见 §2） | 命令行 | 重启 |
| 上传大小、扩展名白名单（`LOVIA_MAX_UPLOAD_MB`、`LOVIA_UPLOAD_ALLOWED_EXT`） | 服务（部署限制） | 环境变量，不变 | 重启 |
| 默认模式、auto-allow 包 | 设置 | `config.json` 的 `workspace` 节 | 默认模式只影响新对话；auto-allow 从下次运行起生效 |
| max turns、max retries、follow-ups | 设置 | `config.json` 的 `run` 节 | 下次运行 |
| memory 开关和目录、subagents 开关、自定义指令 | 设置 | `config.json` 的 `agent` 节 | 下次运行 |
| 最大输出 tokens | 设置 | 每个模型配置的 `max_tokens` 字段 | 下次运行 |
| 对话的目录 | 对话 | `chat_sessions.workspace_root` | 创建时选定，不再变 |
| 对话的模式 | 对话 | `chat_sessions.workspace_mode` | 下次运行 |

```json
{
  "models": [{ "id": "main", "model": "openai:gpt-5", "max_tokens": 8192 }],
  "workspace": { "default_mode": "coding", "auto_allow": ["read-only", "dev-tools"] },
  "run": { "max_turns": 50, "max_retries": null, "followups": true },
  "agent": { "memory": true, "memory_dir": "./.lovia/memory", "subagents": true, "instructions": "" }
}
```

几点说明：

- **“下次运行”指新开的一轮运行。** 在回复过程中发出的消息只是注入当前这一轮，仍然按旧设置执行。
- **自定义指令**是服务级的系统提示，有没有工作区都生效。第三期起，再叠加各工作区的 `AGENTS.md`；在那之前，照旧读启动目录的 `AGENTS.md`。
- **`max_tokens` 是模型配置里的普通字段**，用户不需要了解各供应商 `extra_body` 的写法。

`build_default_agent()` 改为由 `WebConfig` 加部署约束来建。启动和 `apply()` 共用这条路径。`apply()` 在换 Agent 的同时，把 `max_turns`、`retry`、`followups` 写回 `RouterDeps`。

**`config.json` 是初始配置，不是锁。** 文件可写时，设置页的修改会写回文件；启动时如果发现文件不可写（比如只读挂载），设置页整体只读，并说明原因，而不是等到保存时才失败。部署方要锁定什么，用 §2 的命令行参数。

### 2. 部署方式

所有部署约束都写在这张表里，其他章节不再重复。规则都在服务端执行，覆盖所有写入口，而不只是隐藏界面控件。

先确定**有效启动目录**，再套用锁：

1. 命令行给了 `DIR`，就用 `DIR`，主目录也照用（`lovia web ~` 是用户明确的选择）。
2. 没给 `DIR`：用启动目录。如果启动目录是主目录或根目录，就改用通用工作区 `~/.lovia/workspace`（见 §5），并在启动摘要里说明。

| 启动方式 | 能开的目录 | 能选的模式 | 浏览器能改的 `workspace` 设置 |
| --- | --- | --- | --- |
| 回环地址（默认） | 任意目录，加通用工作区 | readonly、coding、trusted | 全部 |
| 回环 + `--lock-workspace` | 只有有效启动目录 | readonly、coding、trusted | 全部 |
| 非回环地址（默认） | 只有有效启动目录 | 不高于启动时的默认模式，且不含 trusted | 不能改；改磁盘上的文件后重启 |
| 非回环 + `--allow-workspace-switch` | 任意目录，加通用工作区 | readonly、coding | 全部 |
| 任意地址 + `--readonly` | 按上面对应的行 | 只有 readonly | 默认模式和 auto-allow 显示为“由 --readonly 固定”，`config.json` 里的值保持不动 |
| `--no-workspace` | 无 | 无 | 隐藏 |

补充：

- trusted 只在回环地址上提供，`--allow-workspace-switch` 也不会放开它。
- 模式的高低顺序是 readonly < coding < trusted。
- `--allow-workspace-switch` 和 `--lock-workspace` 同时给时，以 `--lock-workspace` 为准。
- 模型、搜索、运行参数这类设置，在任何部署方式下都可以从浏览器修改，和现在一样。
- 现有的 `_warn_if_exposed`（`__main__.py:368`）改为按“能选的最宽模式”来判断。

### 3. 命令行和环境变量

设置页能改的，就不再留命令行参数和环境变量。这样一个值只有一个来源，也不需要给设置页的每个字段标注“被哪个参数锁定”。模型连接已经是这样做的（`config/schema.py` 文首）。

删掉的参数，都在**同一期**提供替代入口：

| 删除 | 替代 |
| --- | --- |
| `--trusted`、`LOVIA_WORKSPACE_MODE` | 设置 → 工作区 → 权限模式（第二期起也可在对话里切换） |
| `--auto-allow`、`LOVIA_AUTO_ALLOW` | 设置 → 工作区 |
| `--max-turns`、`--max-retries`、`--no-followups`（及环境变量） | 设置 → 智能体 |
| `--memory-dir`、`--no-memory`、`--no-subagents`（及环境变量） | 设置 → 智能体 |
| `--instructions`、`--instructions-file`、`LOVIA_INSTRUCTIONS_FILE` | 设置 → 智能体 → 自定义指令 |
| `--max-tokens`、`LOVIA_MAX_TOKENS` | 设置 → 模型 → 最大输出 tokens |

保留下来的：

```
lovia web [DIR]  --readonly | --no-workspace  --lock-workspace  --allow-workspace-switch
                 --host --port --root-path --token --db --title --log-level
                 --provider-timeout --trust-env --app --check
```

`--workspace DIR` 保留为 `DIR` 的别名。

**旧参数和旧环境变量都直接报错，并指出新位置**，例如：`LOVIA_MAX_TURNS → Settings → Agent (config.json run.max_turns)`。环境变量在启动时检查，不能悄悄失效。这张对照表保留到 0.9 系列结束，0.10 删除。

### 4. 对话绑定

`chat_sessions` 加两列：

```sql
ALTER TABLE chat_sessions ADD COLUMN workspace_root TEXT;  -- 解析后的绝对路径；NULL = 未关联（仅限旧对话，§8）
ALTER TABLE chat_sessions ADD COLUMN workspace_mode TEXT;  -- readonly / coding / trusted；NULL = 继承基础 policy
```

**目录一律取对话的绑定。** 新对话创建时就写入实际的绝对路径。

**模式：**

- 显式的模式名：用 `build(root, mode)` 建工作区。CLI 新建对话时总是写入显式模式（取当时的默认模式），所以以后修改设置里的默认模式，不会影响已有对话。
- NULL：把 Agent 自带工作区的 policy 原样搬到绑定的目录上（`replace(root=...)`），嵌入方自定义的规则全部保留。只有嵌入方的 Agent 没有模式名可写时，才会出现 NULL，界面上显示为“默认”。

**CLI 的 `build` 每次调用都读当前的 `auto_allow`。** 所以“默认 coding”和“手动选 coding”得到的规则完全一样；改了 auto-allow，所有 coding 对话都从下次运行起生效。

**一个函数，所有入口都调用。** `deps.effective_agent(agent, binding)` 负责换出工作区，并按 §2 的**当前**约束校验绑定。目录不符合约束时返回 409 `workspace_not_allowed`，提示里写明是哪条约束，不会换目录（第三期）；模式超出范围时降到允许的最宽模式，只收紧、不放宽（第二期起，与第一期 `--readonly`、非本机地址的处理一致）。第二期目录固定，绑定放在 `supervisor.start/start_resume` 里做（取完注册名之后）：只换 policy 不影响附件读取；第三期换目录时再前移到读附件之前。凡是要拿 Agent 处理这个对话的地方，都在读附件、建 `run_deps` 之前调用它：

| 入口 | 位置 | 绑定从哪来 |
| --- | --- | --- |
| `/api/chat`、`/api/chat/stream` 新开一轮 | `api/chat.py` 的 `resolve_agent()`，在 `user_content()`（`chat.py:180`）之前 | 对话的两列；新对话用草稿绑定（§5） |
| 恢复运行 | `api/chat.py:424` | 对话的两列 |
| 往正在运行的对话注入消息 | `api/chat.py:276` 已经用 `ctrl.agent` | 不重新解析，沿用当前运行的快照 |
| 子代理 | `subagents.py` 的 `run_child()`，在 `build_run_deps` 之前 | 父运行实际使用的绑定（`ctrl.agent`），同时写进子会话的两列 |
| 定时任务 | `scheduler.py:218` | 对话的两列；每次新开对话的定时任务，用任务创建时记下的绑定 |
| fork | `api/sessions.py` | 抄父对话的两列 |
| 文件面板、上传 | `api/workspace.py`（`require_cfg`，`:218`） | `?session=` 的两列；草稿用 `?root=`（§5） |

子代理必须走这个函数：`build_default_agent()` 给子代理单独存了一份启动时的工作区（`builder.py:391`），父 Agent 换了工作区，它不会跟着换。

**切模式的时机。** 只从下次运行开始生效。运行进行中切换时，提示“正在进行的回复仍按原模式完成”；已经启动的后台进程和子任务按原来的模式继续跑，要立刻收紧就先停掉它们。这和模型热切换的语义一致。

**切模式不杀后台进程。** `WorkspaceSessions.bind()` 发现配置不一致时会关掉会话重开（`workspaces.py:77`），而 `LocalWorkspaceSession` 在 `open()` 时把 policy 抄进了自己（`workspace.py:116`）。改法：给 `LocalWorkspaceSession` 加 `set_policy(policy)`，只在两次运行之间调用；`bind()` 发现只有 policy 变了时调用它、复用原会话，其他字段变了才重开。

**目录在对话内不变。** 换目录后，转录里的相对路径、后台进程和授权记录都会指向旧目录。要换目录就开新对话。

### 5. 新对话、草稿和通用工作区

**新对话继承当前标签页选中的工作区。** 这是前端状态：每个标签页各自记住，服务端不维护全局的“最近一次选择”。标签页第一次打开时，用有效启动目录（§2）。

**草稿在创建时就确定工作区。** 输入框里的草稿持有 `{root, mode}`：

- 上传文件时显式带上这个目录：`POST /api/workspace/upload?root=...`，服务端用 §4 的同一套校验。文件面板浏览草稿时也一样。
- 草稿里已经有附件、或者正在上传时，工作区选择器禁用；要换目录，先移除附件。
- 第一次发送时，`ChatRequest.workspace` 带上草稿的绑定，服务端校验后写入新对话。不需要为此提前建一条空的对话记录。

**输入框始终显示实际的工作区名称**，悬停显示完整路径，避免用户在不知情的情况下在别的目录里干活。

**通用工作区**是 CLI 托管的 `~/.lovia/workspace`，用于不属于任何项目的对话（随便问问、写文档、处理上传文件），这样生成的文件不会混进项目仓库。

- 只要“能开的目录”包含它（§2），它就固定在工作区列表里，不能移除。
- 只有在没给 `DIR`、且启动目录是主目录或根目录时，它才会成为有效启动目录。其他情况下，它只是列表里的一项，不会被默认选中。
- 第一次用到时才创建目录。

库不会自己建目录：嵌入方的有效启动目录就是 Agent 自带的工作区。

### 6. 库 API

下面是提案中的接口（第二、三期实现），现在还不存在，所以示例按纯文本给出，不参与文档检查。

```text
from lovia.web import create_app, WorkspaceSwitch

app = create_app(agent)                                                   # 固定：和现在一样
app = create_app(agent, workspaces=WorkspaceSwitch())                     # 任意目录，readonly / coding 可切
app = create_app(agent, workspaces=WorkspaceSwitch(roots=["/srv/repos"])) # 只能开这下面的目录
app = create_app(agent, workspaces=WorkspaceSwitch(roots=None))           # 目录固定，只切模式
```

```text
@dataclass(frozen=True)
class WorkspaceSwitch:
    # "any"：任意目录；列表：只能是这些目录或其子目录；None：固定为 Agent 自带的根目录。
    roots: Literal["any"] | Sequence[str | Path] | None = "any"
    # 界面提供哪些模式。trusted 只在回环地址上生效（§2）。
    modes: Sequence[WorkspaceMode] = ("readonly", "coding")
    # 管工作区：按 (根目录, 模式) 建。默认用 Workspace.local(root, mode=mode) 的预设，
    # 沿用 Agent 自带工作区的 env、limits、executor、inherit_env、shell_timeout，
    # 但不会带上自定义的 policy 规则。
    build: Callable[[Path, WorkspaceMode], LocalWorkspace] | None = None
    # 管项目相关的其余部分：指令、插件（比如读该目录的 AGENTS.md 和技能）。
    agent_for: Callable[[Agent[Any], Path], Agent[Any]] | None = None
```

`effective_agent` 的执行顺序是固定的：

1. 按 §2 和 `roots`、`modes` 校验绑定。
2. `agent_for(base, root)`：只管指令和插件。
3. **最后设置工作区**：模式为 NULL 时用基础 policy 换到绑定目录上，否则用 `build(root, mode)`。工作区最后设置，所以 `agent_for` 改了也会被覆盖，最终生效的工作区一定是这一步建的。

改过 policy 的嵌入方，如果希望切模式后自定义规则还在，就自己传 `build`，例如 `lambda root, mode: Workspace.local(root, mode=mode, denied_paths=("secrets/**",))`。从任意 policy 反推模式名、或者自动合并预设和自定义规则，都没有可靠的定义，所以不做。

不做缓存：`agent_for` 每次运行调用一次，等有了性能证据再加。

不传 `workspaces=` 时，`effective_agent` 原样返回 Agent，`/api/info` 里的 `features.workspace_switch` 为 `false`，前端不显示切换控件。

### 7. HTTP 接口和界面

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/workspaces` | 工作区列表，以及按 §2 算出的能开的目录、能选的模式、能改的设置 |
| `POST` | `/api/workspaces` | 添加目录：解析符号链接，确认是目录且在允许范围内 |
| `DELETE` | `/api/workspaces/{id}` | 从列表移除（不删文件，对话保留；通用工作区不能移除） |
| `GET` | `/api/workspaces/dirs?path=` | 添加目录时的补全：只返回子目录名，只限允许范围内 |
| `PATCH` | `/api/sessions/{id}` | 新增 `workspace_mode`；未关联的对话可以设 `workspace_root`（§8） |
| `POST` | `/api/sessions/associate` | 把一批未关联的对话关联到某个目录（§8） |

- `ChatRequest.workspace` 只在新建对话时生效，规则和现有的 `agent` 字段一样。
- 会话列表的每一项都带上 `workspace_root`，侧栏按它分组。
- 工作区列表存在新表 `chat_workspaces(id, root, name, last_used_at)` 里，这是界面状态，不进 `config.json`。
- 新增 SSE 事件 `session_workspace_changed`，让多个标签页的模式显示保持同步。

原型需要画这些状态：

- 侧栏：工作区分组、添加入口（含超出允许范围的报错）、“未关联”分组和批量关联。
- 输入框：工作区名称和完整路径；模式下拉，含锁定态和“当前 / 下次运行”两种状态；有附件时工作区不能切的状态。
- 设置页：新增的三节和模型的最大输出 tokens；被部署约束固定的字段；`config.json` 不可写时的只读态。

### 8. 旧对话和数据库位置

**不自动回填。** 两列在第二期加入，旧对话的 `workspace_root` 全部为 NULL，表示“未关联”。数据库里没有能证明整个对话属于哪个目录的记录：`chat_grants.context` 记的是授权那一刻正在执行的 Agent 的根目录，handoff 之后就是目标 Agent 的（`supervisor.py:458`）。所以授权记录只拿来在选择目录时排在推荐位置，不作为自动绑定的依据。

**未关联的对话第一次继续之前，要选一次目录：**

- 第二期只有一个固定目录，旧对话照旧在这个目录里运行，行为和升级前完全一样，所以第二期不写入、不确认；关联推迟到第三期能切换目录时再做。
- 第三期可以从工作区列表里选目录，同样支持批量关联。
- 只是查看未关联的对话，不需要关联。

**数据库位置。** 第三期把 CLI 默认的数据库改为 `~/.lovia/lovia.db`。如果没给 `--db`，而启动目录下已经有 `./.lovia/lovia.db`，就继续用它，在启动摘要里打印实际路径，不自动迁移。

### 9. 哪些跟着工作区，哪些是全局的

| 东西 | 归属 |
| --- | --- |
| `AGENTS.md` | 工作区（第三期起；之前读启动目录） |
| `.agents/skills` | 工作区，加上用户级的 `~/.agents/skills`（`SkillsConfig.dirs` 里的相对路径改为相对工作区根目录解析） |
| 自定义指令 | 全局（`config.json`） |
| 聊天记录、长期记忆、`config.json` | 全局 |

## 不做

- 对话中途换目录。
- 运行中途换 policy。
- 每个工作区一套 `config.json`。
- 服务级开关热加载。
- 从 policy 反推模式名，以及预设和自定义规则的自动合并。
- `agent_for` 缓存。

## 分期

每期一个 PR，各自可以单独发版，发出去的每一期都是完整的：

1. **设置热加载和命令行精简。** `config.json` 新增三节，模型配置加 `max_tokens`；`apply()` 写回 `RouterDeps`；删掉的参数和环境变量报错并指出新位置，替代入口同期上线；§2 中与单工作区有关的部分（非回环地址不能改 `workspace` 节，`--readonly` 固定显示）；`config.json` 不可写时设置页只读。
   验收重点：
   - 保存配置后，下次运行生效，当前运行保持原设置。
   - 部署限制在服务端执行，界面准确显示限制。
   - 删除的参数和环境变量明确报错并指出新位置。
   - 只读的配置文件能正常启动，设置页清楚说明为什么不能修改。
2. **对话级模式。** 新增两列，新对话写入绝对路径和所选模式；旧对话的 `workspace_root` 留空，照旧运行；按对话模式绑定工作区（supervisor 里，见 §4），子代理、派生对话和定时任务沿用发起运行的模式；`set_policy()`；输入框的模式按钮；`WorkspaceSwitch(modes=..., default_mode=..., build=...)`（`roots` 留到第三期）。
3. **多工作区。** `roots`；通用工作区；草稿绑定和带目录的上传；`chat_workspaces` 表和 `/api/workspaces*`；侧栏分组和批量关联；`agent_for` 按目录读 `AGENTS.md` 和技能；数据库默认位置改到用户目录。
