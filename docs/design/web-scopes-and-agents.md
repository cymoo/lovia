# 多工作区之后：每份状态归谁，以及自定义 Agent

> 状态：评审通过，作为实现基线；第 1 期（§1、§2、§6、§8）已实现（v0.9.179），第 2 期（§3–§5）已实现（v0.9.180），第 3 期（§7 的 core 部分）已实现（v0.9.181），两期实现时定下的接缝见文末；第 4 期未开始 · 2026-11-12 · 范围：`lovia/web`（配置、CLI、构建、设置页、composer、记忆对话框）、core 的 `plugins/memory`（加作用域）
>
> 行号指向 `main`（`0422800`，v0.9.178）。

## 结论

`lovia web` 原来只有一个工作区，所以技能、指令、记忆、配置"归谁"没有歧义：都归启动目录。多工作区（#364 起）和远程工作区（#392 起）之后，对话可以在任何目录里进行，但有几样东西还绑在启动目录上，另有几样东西全局共用却带着项目的内容。

改法是一条规则：

**每份状态只有一个归属——服务、项目、对话三者之一——并且没有任何东西按进程的当前目录解析。**

| 归属 | 内容 | 在哪里 | 在界面的哪里 |
| --- | --- | --- | --- |
| 服务（home） | 连接（模型、搜索服务）、全局技能目录、Agent 定义、聊天库、用户笔记 | 一个目录，默认 `~/.lovia`，`--home DIR` 可换 | 设置页 |
| 项目 | `AGENTS.md`、`.agents/skills`、项目笔记、项目的历史对话检索 | 前两样在项目目录里；后两样在 home 里按项目分开 | 文件面板、技能列表的"项目"分组、记忆对话框的"本项目"标签 |
| 对话 | 工作区、权限模式、用哪个 Agent | 聊天库 | composer 底部的三个 chip |

由此得出六项改动：

1. **一个 home**。配置、聊天库、记忆都在 home 里。启动目录只决定新对话的默认工作区。
2. **技能分两处**。设置页只管全局技能目录；项目技能是固定约定（`.agents/skills`），不是配置项。
3. **自定义 Agent**。默认带一个，用户可以再建；提示词、模型、默认权限、技能、搜索开关、轮数、追问建议都是 Agent 的属性。它取代"每个项目一份配置"的需求。
4. **设置页重排**为五页：通用、连接、技能、Agent、关于。权限页和搜索页并入 Agent 页和连接页。
5. **本机与非本机访问规则一致**。服务端不再根据绑定地址改变行为；要收紧就用显式参数。
6. **记忆加作用域**。用户笔记全局；项目笔记和 `recall` 按项目。

另有一个小改动随第 1 期发：web 的默认最大轮数从 50 改为 100。

## 现状

| 配置项 | 现在按什么解析 | 问题 |
| --- | --- | --- |
| `AGENTS.md` | 对话的工作区，每次运行读取（`builder.py:299`、远程 `builder.py:329`） | 无 |
| 技能，相对路径项 | 对话的工作区（`builder.py:371`）；远程项目的不加载（#405） | 设置页里增删它，影响的是所有项目，但页面按启动目录展示（`api/config.py:476`） |
| 技能，绝对路径项 | 全局 | 无 |
| 子代理、定时任务 | 跟随对话的工作区 | 无 |
| 聊天库 | `~/.lovia/lovia.db`；启动目录里已有 `./.lovia/lovia.db` 时沿用它（`__main__.py:379`） | 启动目录影响用哪个库 |
| `config.json` | 启动目录的 `./.lovia/config.json` 整文件覆盖 `~/.lovia/config.json`（`config/storage.py:93`） | "项目级"实际是"启动目录级"；在别的项目里对话时，那个项目的配置不会被读 |
| 记忆 | `./.lovia/memory`，相对进程当前目录（`config/schema.py:206`、`builder.py:171`） | 从哪里启动就用哪份；同一份笔记注入所有项目的对话；`recall` 搜到所有项目的历史 |
| 权限默认、轮数、提示词等 | `config.json` 的 `workspace`、`run`、`agent` 三段，全局一份 | 不同工作想要不同设置时无处表达 |
| 权限上限 | 按是否本机访问分支（`builder.py:85` 的 `Deployment`） | 容器里必须绑 `0.0.0.0`，但使用者就是部署者本人，却选不了 trusted |

记忆插件的整理提示词会把"用户的项目的长期细节"记进笔记（`plugins/memory/plugin.py:373` 的 `_DIGEST_INSTRUCTIONS`），所以项目事实确实会串到别的项目。

## 其他 harness 的做法

只取与本设计有关的事实，供对照，不当作规范。

| | 用户层 | 项目层 | 记忆 | 多项目时 |
| --- | --- | --- | --- | --- |
| Claude Code | `~/.claude/` | `.claude/`、`CLAUDE.md`、`.mcp.json` | 在家目录里按 Git 仓库分开（同仓库的子目录和 worktree 共用） | 每个会话绑定一个目录；设置按键合并 |
| Codex | `~/.codex/config.toml`、`~/.agents/skills` | `.codex/config.toml`、`AGENTS.md`、`.agents/skills` | `~/.codex/memories/`，官方未承诺按项目隔离 | 项目配置只在项目被信任后加载，不能覆盖 provider 和认证 |
| OpenCode | `~/.config/opencode/` | `opencode.json`、`.opencode/` | 无内置 | 服务端每个请求带目录，每个目录一个实例；可定义多个主 Agent，各有提示词、模型、权限 |
| Cursor | 用户规则在账号设置里 | `.cursor/rules`、`.cursor/mcp.json` | 按项目 | 按打开的项目 |

取用的两点：项目层按会话所在目录解析，不按进程启动目录；OpenCode 用"多个可选的 Agent"而不是"每个项目一份配置"来表达不同的工作方式。"密钥只放用户层"不是各家共识（OpenCode 的项目配置可以引用密钥），在本设计里它是 lovia 自己的规定。

## 设计

### 1. 一个 home

```
<home>/                默认 ~/.lovia，lovia web --home DIR 可换
  config.json          连接、全局技能目录、Agent 定义
  lovia.db             聊天库（--db 仍可显式指定别处）
  memory/              见 §7
  workspace/           "无项目"对话的工作区
```

- **新增 `--home DIR`**。不加环境变量（`LOVIA_MEMORY_DIR` 这类已在此前移除，保持一致）。
- **删除三处启动目录的隐式行为**：`./.lovia/config.json` 优先、`./.lovia/lovia.db` 自动沿用、`./.lovia/memory` 默认值。
- **删除 `agent.memory_dir`**。记忆固定在 `<home>/memory`；要放别处就换 home。旧文件里的这个键被忽略（模型是 `extra="ignore"`）。
- **启动目录只剩一个作用**：新对话的默认工作区（已是现状，`__main__.py:363`）。
- **隔离的单位是 home**。测试、互不相干的客户各用一个 `--home`。

迁移：`lovia web --home ./.lovia` 等于以前在该目录启动的行为。启动时若发现 `./.lovia/config.json` 而没有传 `--home`，打一行日志指出这一点。旧记忆文件不迁移。

`create_app` 的库用户不受影响：home 是 `lovia web` 这个 CLI 的概念，`create_app` 的 `store`、`agent` 仍由调用方给。

### 2. 技能

- **`skills.dirs` 只接受全局路径**（绝对路径或 `~` 开头）。默认值是 `["~/.agents/skills"]`。相对路径项在校验时拒绝，并提示项目技能是自动加载的。
- **项目技能是约定**：工作区根目录下的 `.agents/skills`，与 `AGENTS.md` 同等地位，写成常量，不进配置。同名时项目的优先。
- **远程项目的技能**经工作区会话读取（#405）。在它实现之前，远程对话只有全局技能，界面如实显示。
- **设置页的技能页**只列全局目录和其中的技能。项目技能在使用处按"项目 / 全局"分组显示（输入 `/` 或选技能的地方）。

这样配置里不再有含义随对话变化的相对路径。

### 3. 自定义 Agent

服务端和页面已经支持多个 Agent（`/api/agents`、对话记录的 `agent` 字段、定时任务按 Agent），只是 `lovia web` 目前只造一个。改动是让 `config.json` 能定义多个，设置页能增删改。

**配置结构**（`version: 2`）：

```json
{
  "version": 2,
  "models": [{ "id": "main", "model": "anthropic:claude-fable-5-2", "api_key": "…" }],
  "roles": { "chat": "main", "vision": null, "aux": null },
  "search": { "backend": "auto", "tavily_api_key": null },
  "skills": { "dirs": ["~/.agents/skills"] },
  "agents": [
    {
      "id": "lovia",
      "name": "lovia",
      "description": "",
      "instructions": "",
      "model": null,
      "mode": "coding",
      "auto_allow": ["read-only", "dev-tools"],
      "skills": null,
      "search": true,
      "memory": true,
      "subagents": true,
      "max_turns": 100,
      "max_retries": null,
      "followups": true
    }
  ]
}
```

- **`id` 是稳定的标识**，对话和定时任务存的是它；`name` 是显示名，可以随便改。
- **`model: null`** 用 `roles.chat` 指向的模型；否则填一个模型的 `id`。
- **`skills: null`** 继承全局技能目录；填列表则只用这些目录。项目技能始终加载。
- **`agents` 为空或缺失**时，按上面的默认值合成一个 `lovia`。列表第一个是默认 Agent。
- **原来的 `agent`、`run`、`workspace` 三段**的内容成为默认 Agent 的字段。旧文件不折算：`models`、`roles`、`search` 的键没有变，照常读入；那三段被忽略（模型是 `extra="ignore"`），提示词、权限默认、轮数回到默认值，下次保存写成 `version: 2`。多数用户没有改过这三段，不值得为它们写一段很快要删的迁移代码。发布说明里写明。

**哪些归 Agent，哪些仍全局**：

| 每个 Agent 自己的 | 全局的 |
| --- | --- |
| 名称、说明、提示词 | 模型列表和密钥 |
| 用哪个模型 | 搜索服务的后端和密钥 |
| 默认权限模式、免确认命令组 | 全局技能目录 |
| 技能目录（默认继承全局的） | home 的位置 |
| 搜索、记忆、子代理的开关（只这三个） | `--readonly` 等部署限制 |
| 最大轮数、重试次数、追问建议 | 用户笔记（所有 Agent 共用） |

规则是：**连接和密钥是全局资源，配一次；Agent 引用它们并决定用不用。**

**Agent 与项目是两条轴**：Agent 是"谁来干"，全局定义、用户选；项目是"在哪干"，上下文来自目录。记忆不按 Agent 分。为了不让用户给每个项目建一个 Agent 来模拟项目配置，新对话默认沿用该项目上次用的 Agent。

**已有规则不变**：一个对话始终用创建它时的 Agent；那个 Agent 不在了就回落到默认 Agent（`api/chat.py:138` 的 `resolve_agent`）。本设计只补一个页面提示。

### 4. composer 底部的 Agent chip

composer 底部一行已有项目和权限模式两个 chip。Agent 作为第三个，用同样的弹出菜单，顶部的 Agent 下拉框（`templates/index.html:71`）去掉。

- **新对话**：chip 可点，默认是该项目上次用的 Agent，发第一条消息时生效。新对话的权限模式取所选 Agent 的默认模式。
- **进行中的对话**：chip 只显示，不可点。
- **只有一个 Agent 时**：chip 不显示。

对话中途换 Agent 本期不做。最常见的中途切换需求（先只读规划，再放开写）已由权限模式 chip 覆盖。以后要做时需要：请求显式表达"换 Agent"、对话里留一条分隔线、权限模式跟对话走不跟 Agent 走。

### 5. 设置页

| 页 | 内容 | 对比现在 |
| --- | --- | --- |
| 通用 | 语言、主题等 | 不变 |
| 连接 | 模型；搜索服务（后端、Tavily key） | 原"模型"页改名，并入原"搜索"页 |
| 技能 | 全局技能目录及其中的技能 | 不再显示项目技能 |
| Agent | Agent 列表；每个的提示词、模型、权限、技能、开关、轮数 | 并入原"权限"页和原"智能体"页 |
| 关于 | 版本信息 | 不变 |

以后 MCP 服务器进配置时，是"连接"页的第三节。

### 6. 本机与非本机访问规则一致

`Deployment`（`builder.py:85`）现在有五个字段，其中 `loopback` 和 `allow_workspace_edit` 让行为随绑定地址分支：非本机时不给 trusted、不让换目录、不让改权限设置、不让开远程工作区。

这些分支挡不住它们想挡的东西：

- 绑定地址不能说明浏览器前是不是部署者本人。容器里必须绑 `0.0.0.0`。
- 非本机时 coding 模式本来就可用，而命令的批准也是在浏览器里点的。持有令牌的人已经能批准自己发起的命令，包括一条 `ssh`。trusted 比 coding 只少了弹窗；凭据目录等基线拒绝规则在 trusted 下照样生效。

改法：

- **删除** `Deployment.loopback`、`Deployment.allow_workspace_edit`，以及 `--allow-workspace-switch` 参数。`roots()`、`remotes()`、`modes()`、`chat_modes()`、`workspace_lock()` 不再看绑定地址。
- **保留**部署者显式写出的限制：`--readonly`（所有运行只读）、`--lock-workspace`（所有对话留在启动目录，也不开远程）、`--no-workspace`。
- **不变**：非本机绑定必须有令牌（没给就生成并打印）；绑定非本机且允许写和 shell 时的启动警告保留，措辞改为建议用上面三个参数。

这推翻了两个此前的决定——"trusted 仅限本机"和"`lovia web` 只在本机访问时开远程工作区"（`docs/design/remote-workspace.md`）。理由如上；合入时同步修改那两份文档。

### 7. 记忆的作用域

**目标**：关于用户的事实在所有项目里可见；关于某个项目的事实和它的历史对话，只在那个项目里可见。

**core 的改动**：`Memory` 加两个字段。

```python
Memory(
    "~/.lovia/memory",
    scope=lambda ctx: project_key(ctx),   # (RunContext) -> str | None
    scope_name="project",                  # 提示词里怎么称呼这个范围
)
```

- `scope` 为 `None`（默认）时，行为与现在完全相同：一份笔记、一个归档。
- `scope` 返回一个键时，这次运行多出一层按键分开的存储。返回 `None` 表示这次运行不属于任何范围。
- `scope` 只支持默认存储：与 `notes=` 或 `index=` 同时传就报 `UserError`，而不是让范围层悄悄落到 `root` 下的默认文件里。`embedder=` 不受影响，两层都生效。有人需要自定义的范围层存储时，再加一个按键造存储的工厂参数。

**存储布局**（默认存储）：

```
memory/
  MEMORY.md                 用户笔记：注入每次运行
  archive.db                不属于任何范围的运行的历史
  scopes/<key>/MEMORY.md    范围笔记：只注入该范围的运行
  scopes/<key>/archive.db   该范围的历史：recall 只搜这里
```

归档按范围分文件，所以 `Index` 协议（`add` / `remove` / `search`）不用加过滤参数。

**行为**：

| | 没有范围的运行 | 有范围的运行 |
| --- | --- | --- |
| 注入提示词 | 用户笔记 | 用户笔记 + 范围笔记，分两节，各用 `notes_budget`（已定：提示词里的笔记最多是现在的两倍） |
| `remember(fact)` | 写用户笔记 | 多一个参数 `about`：`"user"` 或 `"project"`（取 `scope_name`） |
| `forget(fact)` | 在用户笔记里找 | 两层都找 |
| `recall(query)` | 搜顶层归档 | 只搜该范围的归档 |
| 运行结束的整理 | 事实进用户笔记，摘要进顶层归档 | 整理结果分 `facts`（关于用户）和 `scope_facts`（关于项目）；摘要进该范围的归档 |
| dream | 整理用户笔记 | 两层各自整理，各有自己的节奏标记 |

- **分层的判断交给模型**，提示词给出判据：说的是这个人（偏好、规矩、长期情况）就是用户；说的是这个代码库或这件工作，就是项目。拿不准时归项目，因为范围更窄，错了的代价是换个项目要再说一遍。
- **分错的后果**：用户偏好被困在一个项目里，或项目事实变成全局（即现状）。两者都能在记忆对话框里手工挪。
- **每个会话钉住笔记快照**的机制（`notes_refresh_after`）对两层同样适用；一个会话只属于一个范围。
- **内部结构**：把现在 `Memory` 上的笔记存储、索引、锁、dream 标记收成一个"层"对象；`Memory` 持有顶层的一个，按键惰性创建并缓存范围层。后端仍是长寿命、跨运行共享、插件不关闭。

**web 的接线**：

- `scope` 取对话的工作区标识（`WorkspaceRef.key`：本地是解析后的绝对路径，远程是 `ssh://…` / `docker://…`）。"无项目"工作区返回 `None`。
- 目录名是"末级目录名 + 标识的短哈希"，例如 `lovia-3f9a1c2e`；范围目录里存一个小文件记原始标识，供界面显示。
- 项目按路径识别（已定），不按 Git 仓库：简单，对远程工作区同样适用。代价是目录改名或移动后项目记忆断开，同一仓库的不同 worktree 各有一份。
- 子代理不带记忆（现状）；定时任务的运行属于它所在的工作区。

**记忆对话框**：沿用侧栏底部的入口和现有对话框，加两个标签，用文件面板那种标签条，对话框高度固定：

- **关于我**：用户笔记。
- **本项目（项目名）**：当前对话所在项目的笔记；"无项目"的对话里不显示这个标签。带一个"清空本项目记忆"，确认后同时删掉该项目的笔记和历史归档——笔记可以手工删空，归档用户够不着。

删除项目下的对话不会顺带清记忆：删对话和忘掉从中学到的东西是两个意图。"关于我"不加清空按钮，直接在编辑框里删。

`/api/memory` 的读写和 dream 相应加一个参数指明哪一层。不新建"项目面板"：项目笔记在记忆对话框，项目技能在技能列表的分组里，`AGENTS.md` 在文件面板里本来就能看。

### 8. 默认最大轮数

`config/schema.py:205` 的 `DEFAULT_MAX_TURNS` 从 50 改为 100，作为每个 Agent 的默认值。core 的 `Runner.run(max_turns=50)` 不动：那是库的默认值，调用方自己传。循环保护（LoopGuard）照常生效。

## 不做

- **每个项目一份 `config.json`**（按键合并、信任机制、可覆盖键的白名单）。自定义 Agent 覆盖了它要解决的需求，而且 Agent 是用户在 home 里定义的，没有"仓库带进来的文件说了算"的信任问题。
- **按 Agent 分记忆**。Agent 和项目是两条轴，记忆跟人和项目走。
- **`recall` 跨项目搜索**。需要时再加一个显式参数；默认严格按项目。
- **新建"项目面板"**。见 §7。
- **环境变量形式的 home**。
- **对话中途换 Agent**。见 §4。

## 分期

各期可独立合并。

| 期 | 内容 | 涉及 |
| --- | --- | --- |
| 1 | home（§1）、技能拆分（§2）、访问规则一致（§6）、轮数 100（§8） | `web/__main__.py`、`web/config/`、`web/builder.py`、设置页的技能页和权限页、两份 README 和 `docs/{en,zh}/` |
| 2 | 自定义 Agent（§3）、composer chip（§4）、设置页重排（§5） | `web/config/schema.py`、`web/config/runtime.py`、`web/builder.py`、`settings.js`、`agent-config.js`、composer |
| 3 | `Memory` 的作用域（§7 的 core 部分） | `plugins/memory/plugin.py`、实跑模型的测试用来调提示词 |
| 4 | web 接线和记忆对话框（§7 的 web 部分） | `web/builder.py`、`web/api/memory.py`、`memory.js` |

第 1 期先做，是因为第 2 期的配置结构建在它之上。第 3 期是 core 的公共接口和提示词改动，单独评审。

## 评审中定下的取舍

没有未决问题。下面几条在评审里有过别的选项，记下选了什么和为什么：

| 问题 | 定论 | 没选的 |
| --- | --- | --- |
| 旧配置文件 | 不折算，三段旧设置回到默认值（§3） | 读入时折算：要写一段很快就删的代码，而多数用户没改过这三段 |
| 项目的识别 | 按路径（§7） | 按 Git 仓库：对 worktree 友好，但远程工作区用不上 |
| 两层笔记的预算 | 各用一份 `notes_budget`（§7） | 合计一份：项目笔记会挤掉用户笔记 |
| `scope` 与自定义存储 | 同时传就报错（§7） | 范围层悄悄用默认存储：行为出人意料 |
| Agent 的工具开关 | 只有搜索、记忆、子代理（§3） | 每个内置工具一个开关：没有明确用处；要限制能力，权限模式更有效 |
| 清空项目记忆 | 对话框里给入口；删对话不联动（§7） | 删对话时顺带清：两个不同的意图 |

## 第 2 期实现时定下的接缝

设计没写到实现层、第 2 期动手时定下的几处：

| 问题 | 定论 |
| --- | --- |
| 注册表的键与 `Agent.name` | 都是 `id`（不含点）；显示名、说明、默认模式由 `ConfigRuntime.agent_config(id)` 提供，`AgentInfo` 多了 `label`、`description`、`mode`，嵌入方得到的是 `None` |
| 后台子代理归谁 | 子代理叫 `<id>.sub`，点前面就是它所属 Agent 的 `id`（`builder.spec_id`） |
| 库里的"默认 Agent" | 注册的第一个；`pick(None)` 不再因为有多个 Agent 而报 400。`RouterDeps.agent_of(meta)` 是"对话的 Agent，不在了就用默认"这一条规则的唯一实现 |
| 运行上限 | `retry`、`context_policy` 放在各自的 `Agent` 上；`max_turns` 是 `create_app(max_turns=int \| Mapping[str, int])`；追问开关由 CLI 传入的 `FollowupFn` 按 Agent 判断 |
| 工作区的 `build` 钩子 | 签名改为 `(agent, root, mode)`，CLI 用它取该 Agent 的免确认命令 |
| 共享的东西 | 同一个模型配置只建一个 provider；同一次构建只建一个 `Memory`（它持有笔记文件的锁） |
| Agent 被删除后的定时任务 | 失败并写明是哪个 Agent，不回落到默认：定时运行无人值守，换成另一个提示词和默认模式不该悄悄发生 |
| 删除模型 | 用它的 Agent 改为跟随默认模型；默认聊天模型照旧不能删 |
| composer 的模型 chip | 改的是当前 Agent 用的模型：Agent 指定了模型就改它的，否则改 `roles.chat` |

## 第 3 期实现时定下的接缝

| 问题 | 定论 |
| --- | --- |
| 键 → 目录名 | 在 core 里做：`scopes/<末级名>-<sha256 前 8 位>/`，末级名取 `/`、`\`、`:` 分隔的最后一段，保留 Unicode 字母数字和 `._-`；目录里的 `scope.json` 记原始键。web 只传 `WorkspaceRef.key` |
| 公开方法指明层 | `remember` / `forget` / `notes_body` / `replace_notes` / `dream` / `dreamed_at` 都加关键字参数 `scope: str \| None = None`，`None` 是用户层；没配 `Memory(scope=)` 却传键报 `UserError` |
| 清空一个项目 | `clear_scope(key)`：持该层的锁删目录，再在同一个层对象上重开存储；还在跑的运行写进的是清空后的新存储 |
| digest 输出 | 无范围用原来的 `_RunDigest` 和提示词（逐字节不变）；有范围用子类 `_ScopedRunDigest`（多 `scope_facts`）和两层提示词 |
| `stale` / `forget` 工具 | 两层都匹配，精确、规范化后相同优先于子串，平手归范围层；整理时先拿用户层的锁再拿范围层的，跨层的"删旧加新"也是一次完成 |
| 快照 | 无范围的运行仍按 `session_id`，有范围的按 `(session_id, 键)`：会话换了工作区也不会钉住别的项目的笔记；任一层的主动编辑让全部快照失效 |
| `scope` 求值 | `setup` 时调一次（此时 `ctx.workspace` 已打开，有测试钉住）；之后的工具和整理都用这一层 |
| `index=None` + `scope` | 允许：不涉及自定义存储，两层都没有 `recall` |
| `about` | 必填，取值 `"user"` 或 `scope_name`；`scope_name` 须是一个词且不是 `"user"` |

## 参考

- [Claude Code：memory](https://code.claude.com/docs/en/memory)、[settings](https://code.claude.com/docs/en/settings)
- [Codex：config basics](https://developers.openai.com/codex/config-basic)、[advanced configuration](https://developers.openai.com/codex/config-advanced)、[memories](https://developers.openai.com/codex/memories)
- [OpenCode：config](https://opencode.ai/docs/config/)、[server](https://opencode.ai/docs/server/)
- [Cursor：rules](https://cursor.com/docs/rules.md)
- 本仓库：`docs/design/web-workspace-switching.md`、`docs/design/remote-workspace.md`、`docs/design/permissions.md`
