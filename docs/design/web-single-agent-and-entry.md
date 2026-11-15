# 收回多 Agent 设置，统一入口，项目 MCP，slash 命令

> 状态：评审通过，作为实现基线 · 2026-11-16 · 范围：`lovia/web`（配置、构建、CLI、设置页、composer）。core 不动。
>
> 行号指向 `main`（`d94e2766`，v0.9.207）。本文修订 `web-scopes-and-agents.md` 的 §3–§5；它的 §1、§2、§6–§8 不变。

## 结论

`web-scopes-and-agents.md` 用"自定义 Agent"取代"每个项目一份配置"。上线后看，它没有做到这一点，而它自己带来的概念和歧义不少。本文做五件事，每件一个 PR：

| 期 | 内容 | 一句话 |
| --- | --- | --- |
| 1 | 收回多 Agent 设置 | `config.json` 的 `agents[]` 变成一段 `assistant`；设置页的 Agent 列表变成一页"助手" |
| 2 | 公开成品入口 | 新增 `lovia.web.default_app()`，`lovia web` 变成它的 argparse 包装 |
| 3 | 项目 MCP | 读工作区根的 `.mcp.json`，每个服务器批准一次 |
| 4 | slash 命令 | `/` 菜单在技能之外多一组命令，命令走注册表 |
| 5 | 模型跟对话走 | 每条对话记自己的模型；单独评审 |

库层的多 Agent（`create_app({"a": agent_a, "b": agent_b})`）不动：那是嵌入方在代码里做的事，本文只收回 `lovia web` 的设置。

## 为什么收回

**它没有取代项目配置。** `agents[].mcp` 是一个总开关（`config/schema.py:313`），不能表达"这个项目用这两个 MCP 服务器"。要按项目配，只能一个项目建一个 Agent，而原设计明确不想这样。项目的东西需要项目层的答案（第 3 期）。

**每个字段都有更合适的归属。**

| `AgentConfig` 的字段 | 实际由谁决定 |
| --- | --- |
| `instructions` | 项目的 `AGENTS.md`；跨项目的那几句话全局一份就够 |
| `model` | 对话（第 5 期）；之前是全局默认 |
| `mode`、`auto_allow` | 对话在 composer 里选；设置页只管新对话的默认值 |
| `skills` | 全局目录加项目的 `.agents/skills` |
| `search`、`memory`、`subagents`、`followups` | 全局开关 |
| `mcp` | 每个服务器自己的 `enabled`，加项目的 `.mcp.json` |
| `max_turns`、`max_retries` | 没人按 Agent 调；全局一份，只在文件里 |

**它带来的歧义。**

- Agent 的提示词叠在 `AGENTS.md` 前面（`builder.py:404`），指令有两个来源。
- "新对话沿用该项目上次用的 Agent"（`agent-chip.js:48`）是把 Agent 悄悄绑回项目。
- 对话钉在创建时的 Agent 上；Agent 被删后静默回落到默认 Agent，提示词和默认权限都变了。
- 定时任务的 Agent 被删后直接失败（`scheduler.py:229`）。
- 记忆是共用的，但开关按 Agent：切换 Agent 后"记不记"会变，"记了什么"不变。

**别家的做法。** Claude Code、Codex、opencode、VS Code 的自定义 Agent 都是文件，用途是委派（子代理）或随仓库分发；没有一家把它做成个人设置里的一张表。Cursor 做过 Custom Modes，在 2.1 删了。Codex 的设置页是一份全局默认值（默认权限、审批策略、沙盒、网页搜索）。

**什么时候才需要多个 Agent**：分发给别人、作为被委派的角色、作为安全边界（面向客户的和内部的）。前两个是子代理和插件的事，第三个是嵌入方的事。单用户的 `lovia web` 只剩"无项目的人设"（翻译、写作），用技能或一个带 `AGENTS.md` 的文件夹就能做。

## 第 1 期：收回多 Agent 设置

### 配置结构（`version: 3`）

```json
{
  "version": 3,
  "models": [{ "id": "main", "model": "anthropic:claude-fable-5-2", "api_key": "…" }],
  "roles": { "chat": "main", "vision": null, "aux": null },
  "search": { "backend": "auto", "tavily_api_key": null },
  "skills": { "dirs": ["~/.agents/skills"] },
  "mcp": [],
  "assistant": {
    "instructions": "",
    "mode": "coding",
    "auto_allow": ["read-only", "dev-tools"],
    "search": true,
    "memory": true,
    "subagents": true,
    "followups": true,
    "max_turns": 150,
    "max_retries": 5
  }
}
```

- **这一段叫 `assistant`，和设置页的"助手"同名**。最大轮数和重试次数只能在文件里改，文件里的名字要和界面对得上；也避开 v1 文件里那个含义不同的 `agent` 段。
- **去掉的字段**：`id`、`name`、`model`、`skills`、`mcp`。模型用 `roles.chat`；技能目录用 `skills.dirs`；MCP 用每个服务器的 `enabled`。
- **默认值的两处改动**（已定）：`max_turns` 从 100 改为 150；`max_retries` 从"跟随 core"改为 5。core 的默认是总共 5 次调用，即重试 4 次（`reliability.py`）；这里是重试 5 次，共 6 次调用。core 的 `Runner` 和 `RetryPolicy` 不动。
- **旧文件不折算**（已定，与 v1 到 v2 一致）：`agents` 被忽略（模型是 `extra="ignore"`），`assistant` 段取默认值，下次保存写成 `version: 3`。`models`、`roles`、`search`、`skills`、`mcp` 的键没变，照常读入。发布说明写明：改过指令或默认权限的人要重填一次。- **服务的 Agent 仍叫 `lovia`**（`DEFAULT_AGENT_ID`）。对话存的 `agent` 字段不变；存着别的 id 的对话按已有规则跑在默认 Agent 上（`RouterDeps.agent_of`）。
- **存着别的 id 的定时任务**照已有规则失败并写明原因。不为它加回落：定时运行无人值守，换一套提示词和权限不该悄悄发生。发布说明写明要重建。
### 代码上少掉什么

| 位置 | 现在 | 之后 |
| --- | --- | --- |
| `config/schema.py` | `AgentConfig`（带 id、name、model、skills、mcp）、`agents` 列表、`agent_slug`、`agent_profile`、`agent_skills`、重复 id 校验 | `AssistantConfig` 一段；模型用 `default_profile()`，技能用 `skills` |
| `config/runtime.py` | `build()` 循环建多个、按模型共享 provider、`agent_config(name)`、`_spec(agent)`、`max_turns()` 返回每个 Agent 一项 | 建一个；`max_turns()` 是它和它的子代理两项 |
| `api/config.py` | `/agents` 的增、改、复制、设为默认、删除五个路由 | `PUT /api/config/assistant` 一个 |
| `agent-config.js` | 列表加表单，465 行 | 一个表单 |
| `builder.py` | `project_agent`、`remote_agent`、`resolve_workspace` 都带 `spec` | 读 `config.assistant` |

`agent-chip.js` 和 `/api/agents` 不动：它们是库的多 Agent 界面，只在服务了多于一个 Agent 时出现，`lovia web` 下自然消失。

### 设置页

| 组 | 页 | 内容 | 对比现在 |
| --- | --- | --- | --- |
| 你的 | 通用 | 语言、主题等本浏览器的偏好 | 不变 |
| 你的 | 记忆 | 笔记 | 不变 |
| 服务 | 模型 | 模型列表和角色 | 不变 |
| 服务 | 助手 | 自定义指令；新对话的权限模式和免确认命令组；网页搜索、长期记忆、后台子任务、追问建议四个开关 | 取代 Agent 页 |
| 服务 | 技能 | 全局技能目录 | 不变 |
| 服务 | MCP | 全局 MCP 服务器 | 不变 |
| 服务 | 搜索 | 后端、密钥 | 不变 |
| — | 关于 | 版本 | 不变 |

- **一条措辞规则**：设置页说"新对话的默认值"，composer 说"这条对话"。
- **最大轮数和重试次数不进界面**（已定），只在 `config.json` 里。"助手"页末尾一行写明当前的值、文件路径和 `assistant` 段里的键名，以及改后要重启。
- **在用得着的地方说**：运行因为到了轮数上限而停下时，重试那一行多一个"调整轮数上限"，打开"助手"页。用户是在这一刻才想改它的。
- **四个开关都留在"助手"页**。实现时没有把记忆的开关挪到记忆页：记忆页只在有记忆时才出现，关掉之后就没有地方再打开了。
- 自定义指令的说明写明它和 `AGENTS.md` 的关系：先是这里的，再是项目的。

## 第 2 期：公开成品入口

### 现状

`lovia web` 等于 `create_app` 加一段私有装配（`Deployment`、`ConfigRuntime`、`build_agent`），而 `lovia.web.config` 和 `lovia.web.builder` 在稳定性约定里列为"不保证"。库用户因此拿不到这个成品：

| | `lovia web` | `create_app(agent)` |
| --- | --- | --- |
| 权限模式 chip | 三档 | 默认没有；`WorkspaceSwitch()` 默认只读和 coding 两档 |
| 设置页的服务组、模型 chip、首次配置引导 | 有 | 没有（`features.model_config`） |
| 项目的 `AGENTS.md`、`.agents/skills` 按对话加载 | 有 | 要自己写 `agent_for` |
| 默认工具和插件（待办、定时、子代理、记忆、图表、提问） | 有 | 要自己装 |
| 最大轮数 / 追问建议 / 审批超时 | 150 / 开 / 600 秒 | 50 / 关 / 不超时 |
| 聊天库 | `<home>/lovia.db` | `./.lovia/<agent>.db` |

想要"`lovia web` 的全部，再加我自己的两个工具"，现在做不到。

### 两个入口，各管一件事

- **`create_app(agent)`：原样托管你的 Agent。** 默认值对齐 core，不替嵌入方做决定。页面只显示这个 Agent 用得上的部分。它的默认值一个都不改。
- **`default_app(...)`：`lovia web` 这个成品。** CLI 能做的它都能做，并留一个定制口。

```python
from lovia.web import default_app, serve

def customize(agent):
    return agent.clone(tools=[*agent.tools, my_tool])

app = default_app(workspace=".", customize=customize)
serve(app)
```

```python
def default_app(
    *,
    home: str | Path = "~/.lovia",
    workspace: str | Path | None = ".",   # None：没有工作区（--no-workspace）
    readonly: bool = False,
    lock_workspace: bool = False,
    db_path: str | Path | None = None,     # 默认 <home>/lovia.db
    customize: Callable[[Agent[Any]], Agent[Any]] | None = None,
    title: str = "lovia",
    token: str | None = None,
    auth: Callable[..., Any] | None = None,
    ui: bool | ChatUI = True,
    cors_origins: Sequence[str] | None = None,
    lifespan: Lifespan | None = None,
    run_deps: RunDepsFn | None = None,
    tracer: Tracer | None = None,
) -> FastAPI: ...
```

- **前五个参数就是 CLI 的五个开关**，含义和 `--home`、`--workspace`、`--readonly`、`--lock-workspace`、`--db` 逐一对应。`Deployment` 继续不公开，它的字段成了这几个参数。
- **`customize`** 在每次建出 Agent 后调用（启动时，以及设置页每次保存后），必须返回同名的 Agent（否则报 `UserError`）。主 Agent（`lovia`）和它的后台子代理（`lovia.sub`）各调一次，子代理在装进 `Subagents` 插件之前调；"加一个工具"通常两边都要，只想给一边就按 `agent.name` 分支。它只收 Agent，不收项目目录（已定）：项目的差异由 `AGENTS.md`、技能、`.mcp.json` 表达。
- **其余参数原样传给 `create_app`**，含义相同。`max_turns`、`followups`、`approval_timeout`、`question_channel`、`workspaces`、`config_runtime` 不在其中：它们由成品自己决定（来自 `config.json` 和上面五个开关）。
- **没配模型时**和 CLI 一样服务首次配置页，不报错。
- **`lovia web --app module:app`** 可以指向 `default_app()` 的返回值，CLI 的 host、port、日志参数照常生效。

`__main__.py:615–689` 的那一段装配挪进 `default_app`；`main()` 只剩解析参数、打印摘要、调 `serve`。新增的公开名字只有 `default_app` 一个。

### 顺带的三处

- **`WorkspaceSwitch.modes` 的默认值改为三档都有**（已定）。现在默认是只读和 coding（`workspaces.py:240`），所以嵌入方写了 `workspaces=WorkspaceSwitch()` 后发现页面上没有"完全权限"，这正是两个入口看起来不一致的那一处。`WorkspaceSwitch` 本身是嵌入方主动打开的；按 `web-scopes-and-agents.md` §6 的理由，能在浏览器里批准命令的人已经有完全权限的全部能力，少的只是弹窗。要收紧就显式写 `modes=("readonly", "coding")`。没传 `workspaces=` 的 `create_app` 不受影响，仍然没有模式 chip。
- **`features.model_config` 改名 `features.settings`**（`api/__init__.py:101`），前端的 `canConfigureModels` 相应改名。它现在管着模型、助手、技能、MCP、搜索五页，名字不对；1.0 之后就改不了了。
- **文档**：`docs/{en,zh}/web-server.md` 开头加一节"两个入口"，放上面那张对照表，并写一句怎么选：要托管自己写的 Agent 用 `create_app`，要 `lovia web` 再加点东西用 `default_app`。两份 README 同步。

## 第 3 期：项目 MCP

### 文件

MCP 规范不规定客户端的配置文件。事实上的写法是项目根的 `.mcp.json`（Claude Code；Cursor 的 `.cursor/mcp.json` 同形状），lovia 读它，和 `AGENTS.md`、`.agents/skills` 一样是约定，不是配置项。

```json
{
  "mcpServers": {
    "db": { "command": "npx", "args": ["-y", "@example/db-mcp"], "env": { "DSN": "${APP_DSN}" } },
    "docs": { "type": "http", "url": "https://example.com/mcp", "headers": { "Authorization": "Bearer ${DOCS_TOKEN}" } }
  }
}
```

- **映射到已有的 `MCPServerConfig`**（`config/schema.py:228`）：有 `command` 的是 stdio，有 `url` 的是 streamable HTTP。名字须合已有的模式；`"type": "sse"` 不支持。不合的条目跳过，并在状态里写明原因，不让整个文件失效。
- **`${VAR}` 和 `${VAR:-默认}`** 在 `command`、`args`、`env`、`url`、`headers` 里展开，取服务进程的环境变量。没有默认值又没设的变量：这个服务器不启动，状态里写明缺哪个变量。密钥不写进文件。
- **stdio 服务器的工作目录**是项目根。
- **重名**：项目的优先于全局的，和技能一致。
- **需要 OAuth 登录的远程服务器不支持**：lovia 的 HTTP 传输只带固定的请求头。这样的条目会连接失败，状态里照实写；不在本期加登录流程。
- **每次运行读一次文件**，不缓存，和 `AGENTS.md` 一样；在 `project_agent()`（`builder.py:387`）里把项目的服务器并进 `SettingsMCP`。
- **远程工作区本期不加载**：stdio 服务器该在哪台机器上跑没有定论。界面如实显示，和远程项目技能当初的处理一致。

### 信任

`.mcp.json` 是仓库带进来的、会执行命令的文件。这正是当初不做项目级 `config.json` 的原因，所以要有批准：

- **每个服务器批准一次**，按"项目键、服务器名、定义的哈希"记在 home 里（`<home>/mcp-projects.json`）。哈希取展开前的定义，所以改了命令、参数或地址要重新批准，换了环境变量的值不用。
- **批准记录只在 home 里**。仓库里的任何文件都不能批准自己。
- **Agent 改了 `.mcp.json`** 等于改了哈希，新定义回到待批准。
- **没批准的服务器不启动，运行不等它**（已定）：对话照常开始，composer 上方一行提示"本项目有 2 个 MCP 服务器待批准"，点开是一个对话框，逐个列出完整的命令行或地址，逐个批准或拒绝。拒绝过的不再提示，在 `/mcp` 面板里可以改主意。
- **定时任务和后台子任务**只用已批准的。
- **批准之后**，工具调用的权限照已有规则（`mcp.py:199`）：完全权限下不问，其余模式问一次并可按对话授权。

### 只给这个项目开的服务器（已定）

`.mcp.json` 解决的是"随仓库共享"的那一种。另一种同样常见：只想在这个项目里用某个服务器，但不想把它写进仓库（别人的仓库，或者定义里带着本机路径）。Claude Code 为此有一个单独的 local 作用域；Codex 和 Claude Code 的 `/mcp` 都能按会话开关服务器，因为工具一多就占上下文。

建议用一个概念覆盖这两件事，不加新的配置层：**每个项目记着"哪些服务器在这里开着"**。

- 全局服务器在设置页里有一个默认的开或关（已有的 `enabled`）；在某个项目的 `/mcp` 面板里可以把它单独打开或关掉。
- "只给这个项目用"就是：在设置页加一个默认关着的服务器，在这个项目里打开它。
- 项目 `.mcp.json` 里的服务器默认关着，第一次打开就是上面说的批准。批准因此不是一套单独的机制，只是"打开一个仓库带来的服务器"时多一步确认和一个哈希。
- 记录在同一个文件里（`<home>/mcp-projects.json`），键是项目键和服务器名。

这替掉了被去掉的 `agents[].mcp` 开关，而且比它准：按项目，按服务器。

第 3 期只做批准，但文件格式按"每项目每服务器一个状态"定；开关在第 4 期随 `/mcp` 面板一起做。

### 界面和接口

- 设置页的 MCP 页仍只有全局服务器（设置页是全局的）。
- 项目的服务器在 `/mcp` 命令打开的面板里（第 4 期），按"项目 / 全局"分组，显示状态和批准情况。第 3 期先只做上面那行提示和批准对话框。
- 接口挂在 `/api/config/project/mcp` 下（`/api/config/*` 不在稳定性约定里）：`GET ?session=|root=` 列出项目的服务器、状态和批准情况；`POST` 记一次批准或拒绝。
- `lovia` 本期不写 `.mcp.json`：用户或 Agent 编辑文件。

## 第 4 期：slash 命令

现在 `/` 只列技能（`skill-menu.js`），选中后把 `/name ` 留在输入框里，原样发给模型。命令是另一种东西：选中后立即执行，不发给模型。

### 注册表

```js
window.lovia.registerCommand('ticket', {
  description: '为这个对话创建工单',
  argument: '主题',                          // 可选：名字后面要填的内容
  when: () => lovia.state.sessionId !== null, // 可选：不适用时不列出
  run: ({ args, sessionId }) => { … },
});
```

（这是实现后的形状；评审时写的是 `registerCommand({ name, label, … })` 和 `run({ send, insert })`，改动的理由见文末"实现时定下的接缝"。）

- 和 `registerView`、`registerToolView`、`registerMessageAction` 是同一种扩展方式，嵌入方用它加自己的命令。
- `run` 拿到的只有两样：`args`（命令名后面的文字）和当前对话的 id。要发消息用已有的 `lovia.send`，要填输入框用 `lovia.setDraft`，打开面板用 `openView`。
- **带参数的命令**声明一个 `argument`（占位提示，如"新标题"）：选中后输入框里留下 `/rename `，回车时执行，后面的文字作为 `args`。不带参数的命令选中即执行；它的名字后面还有文字时，那是一条普通消息（"/model 不回答了，帮我看看日志"），照常发给模型。
- 菜单分两组：先命令，后技能。输入时两组各自过滤。
- 命令和技能重名时命令优先，菜单里不再列那个技能。
- **两个入口的页面是同一个注册表**：`create_app` 的页面也有 `/` 命令，只是按 `when` 少列几项。嵌入方加命令的方式和加视图一样。

### 内置命令

只收已有功能的入口，不为菜单新做功能：

| 命令 | 做什么 | 何时列出 |
| --- | --- | --- |
| `/model` | 打开模型 chip 的菜单 | 有设置页时 |
| `/mode` | 打开权限模式 chip 的菜单 | 对话能选模式时 |
| `/mcp` | 打开 MCP 面板：服务器、状态、待批准 | 有设置页时 |
| `/init` | 发出一条内置提示词：通读项目，写出或更新 `AGENTS.md` | 有工作区时 |
| `/memory` | 打开记忆 | 有记忆时 |
| `/rename 新标题`、`/pin`、`/archive`、`/branch` | 对话菜单里已有的四项 | 打开了对话时 |
| `/compact` | 手动压缩 | #427 做完后 |

不收：用量和计费、反馈、加速、绘图、侧边聊天、目标。计划模式仍是 recipe（#173）。⌘K 命令面板此前已否决；内置命令保持在这张表的范围内，不让 `/` 菜单变成它。

## 第 5 期：模型跟对话走

现在 composer 的模型 chip 改的是 `roles.chat`，即全局默认：在对话 A 里换模型，对话 B 的下一条消息也跟着换。收回多 Agent 后，"这件事用便宜的模型"没有别的表达方式，所以模型应当和权限模式一样跟对话走。

- **存储**：`ChatMeta` 加 `model`（模型的 `id`，`None` 表示跟随默认）。新对话在发第一条消息时带上；进行中的对话可以改，下一条消息生效。存着的 id 已不存在时跟随默认，页面提示一次。
- **运行**：每次运行按对话的模型得到一个 Agent。除了 provider，还要跟着换的有 `max_tokens`、按上下文窗口定大小的 `Compaction`、视觉工具（`builder.py:491–520`）。这些现在在 `build_agent` 里一次定死，要改成按模型建一份并缓存。
- **设置页**："模型"页的"设为默认"决定新对话从哪个开始；chip 的说明改为"这条对话"。
- **定时任务**记下创建它的那条对话的模型，之后不跟着默认值变：无人值守的运行不该因为别处换了默认模型而换模型。
- **对话中途换模型**会让 provider 的提示缓存从头再来，跨厂商时尤其明显。记录是 provider 无关的，所以能换；chip 的菜单里写一句代价。
- **库的接缝**：`create_app` 没有模型列表，所以这是成品的功能；通用的那一半是"每次运行前按对话换 Agent"，现有的 `WorkspaceSwitch.agent_for` 只拿到目录，拿不到对话。接缝怎么开是这一期评审的主题。

排在最后并单独评审（已定）。在它之前 chip 保持现状。

## 不做

- **把 `create_app` 的默认值改成和 CLI 一样。** 嵌入方可能在给别人用；完全权限、600 秒超时、追问建议都该由它自己决定。
- **公开 `Deployment`、`ConfigRuntime`、`build_agent`。** `default_app` 一个名字够用；积木公开后都进 1.0 约定。
- **给 `create_app` 的页面加设置页。** 嵌入方在代码里配置。
- **v2 配置的折算、定时任务的 Agent 回落。** 见第 1 期。
- **自定义主 Agent 的文件格式**（`.agents/agents/*.md` 之类）。以后要"角色"，做成子代理的定义。
- **每个项目一份 `config.json`。** 项目层只有约定的文件：`AGENTS.md`、`.agents/skills`、`.mcp.json`。
- **`lovia` 写 `.mcp.json`、远程工作区的项目 MCP、SSE 传输、MCP 的 OAuth 登录。**
- **把 `.mcp.json` 的解析公开成 core 的 API。** 解析和变量展开先放在 `lovia/web` 里；有库用户要用再挪到 MCP 插件旁边公开，那时只是移动代码。
- **存储占用页、用量页。**

## 分期

各期可独立合并，各发一个补丁版本。

| 期 | 涉及 | 依赖 |
| --- | --- | --- |
| 1 收回多 Agent | `config/schema.py`、`config/runtime.py`、`builder.py`、`api/config.py`、`agent-config.js`、`settings.js`、`i18n.js`、两份 README、`docs/{en,zh}/` | 无 |
| 2 成品入口 | `web/__init__.py`、新的 `default_app`、`__main__.py`、`api/__init__.py`、`docs/{en,zh}/web-server.md`、`stability.md` | 1（`customize` 只需面对一个 Agent） |
| 3 项目 MCP | `web/mcp.py`、`config/mcp.py`、`builder.py`、`api/config.py`、composer 的提示行和批准对话框 | 1 |
| 4 slash 命令 | 新的 `commands.js`、`skill-menu.js`、`lovia.js`、MCP 面板 | 3（`/mcp` 面板显示项目服务器） |
| 5 模型跟对话走 | `store.py`、`builder.py`、`config/runtime.py`、`workspaces.py`、模型 chip | 1 |

第 1 期先做：之后每一期的代码都因为只有一个 Agent 而更小。

## 评审中定下的取舍

| 问题 | 定论 | 没选的 |
| --- | --- | --- |
| 旧配置文件 | 不折算，`assistant` 段取默认值（多 Agent 发布才几天，还没有用户） | 读入时取 `agents[0]`，或改写前另存一份 |
| 公开入口的形状 | 一个函数加 `customize` 回调 | 公开积木：公开面大；只补文档：做不到"成品加两个工具" |
| 入口的名字 | `default_app` | `lovia_app`、`assistant_app` |
| 设置页那一页的名字 | "助手"，配置段相应叫 `assistant` | "默认值"、"行为" |
| `customize` 的参数 | 只收 Agent，主 Agent 和子代理各调一次 | 也收项目目录：项目的差异已有约定的文件表达 |
| 待批准的 MCP 服务器 | 不拦运行，只在 composer 上方提示 | 首次运行前拦一下：打断一次本可以开始的对话 |
| 最大轮数、重试次数 | 只在 `config.json`；默认 150 轮、重试 5 次 | 留在设置页 |
| 模型跟对话走 | 做，排最后单独评审 | 不做：并行的对话不能用不同模型；与第 1 期同做：PR 过大 |

| `WorkspaceSwitch.modes` 的默认值 | 三档都有 | 保持两档：嵌入方打开了模式切换却没有完全权限 |
| 按项目开关 MCP 服务器 | 做：记录格式在第 3 期定，开关在第 4 期的 `/mcp` 面板里 | 只做批准：想私用的人得把服务器写进仓库 |

没有未决问题。

## 实现时定下的接缝

| 期 | 问题 | 定论 |
| --- | --- | --- |
| 1 | 四个开关放哪 | 都留在"助手"页：记忆页只在有记忆时才出现，把开关挪过去，关掉后就没有地方再打开 |
| 1 | 保存时带上只在文件里的字段 | `PUT /api/config/assistant` 拒绝未知字段（422），不悄悄丢掉 |
| 2 | `default_app` 放哪 | `lovia/web/default.py`；CLI 从构建好的应用上取回 `ConfigRuntime` 来打印启动摘要 |
| 2 | `customize` 和指令 | 每个对话的基础 `instructions` 都按设置页和 `AGENTS.md` 重新生成，所以 `customize` 里替换它不会保留；指令片段（`agent.instruction(...)`）会保留 |
| 2 | `--app` 托管自定义 Agent 时的默认值 | 仍用 CLI 的姿态（150 轮、追问建议开、600 秒超时），和原来一样 |
| 2 | 相对路径的 `home` 和 `workspace` | 构建时转成绝对路径，之后不随工作目录变 |
| 3 | 在哪一步读 `.mcp.json` | MCP 插件在运行开始时读（`SettingsMCP._for_run`），不是在构建 Agent 时：批准后的下一次运行就生效，不用重建 Agent |
| 3 | 接口路径 | `GET` / `PUT /api/config/project/mcp`（`/mcp/project` 会被已有的 `/mcp/{name}` 抢走） |
| 3 | 批准记录谁能写 | Agent 的工具在任何模式下都不能读写 `<home>/mcp-projects.json`（和 `config.json` 同一条规则）：home 在工作区里时，Agent 否则可以自己写 `.mcp.json` 再自己批准 |
| 3 | 决定之后怎么再改 | 侧栏目录的 ⋯ 菜单里有"项目的 MCP 服务器"，打开同一个对话框；composer 上方那一行只在有未决定的时出现 |
| 4 | 注册的形状 | `registerCommand(name, command)`，和 `registerMessageAction(id, action)` 一样：名字是键，重复注册替换（内置的也能换），返回的函数恢复被替换的那个。`description` 是字符串，没有单独的 `label` |
| 4 | `run` 拿到什么 | 只有 `{ args, sessionId }`。要发消息、填输入框，用已有的 `lovia.send` / `lovia.setDraft`，不再各给一份 |
| 4 | 执行的时机 | 提交时判断：以已注册、此刻可用的命令名开头的消息在页面执行，运行中也立即执行，不进发送队列；其余原样发出。从菜单选中不带参数的命令等于提交 |
| 4 | 重名 | 命令优先，菜单里不再列同名技能。注册时没法知道有哪些技能，所以不做控制台警告 |
| 4 | `/fork` | 叫 `/branch`，和对话菜单里的"分支"一致 |
| 4 | `/init` 在空白页的入口 | 没做：要知道项目里有没有 `AGENTS.md` 得加一个接口，而 `/` 菜单里已经列着 `/init` 和它的说明 |
| 4 | 拆成两个 PR | 注册表和内置命令一个；"按项目开关全局 MCP 服务器"单独一个（`/mcp` 打开的是第 3 期的对话框，后一个 PR 在里面加全局服务器） |
| 4 | 按项目开关全局服务器：存哪 | 同一个 `mcp-projects.json`，多一段 `global`：`{项目键: {服务器名: true/false}}`，和项目自己的那一段分开（两边可以重名）。没有哈希：全局服务器是用户自己添加的，不需要批准 |
| 4 | 和设置里的开关的关系 | 记录的只是"和设置不同"：把开关拨回和设置一样就删掉这条记录，之后跟着设置变 |
| 4 | 插件拿到哪些服务器 | 全部全局服务器（开着的和关着的），每次运行开始时按"项目的记录，否则服务器自己的开关"取。远程项目也按项目开关全局服务器，只是不读它的 `.mcp.json` |
| 3 | `cwd`、`disabled` | 命令在项目目录启动，条目的相对 `cwd` 相对项目解析；文件里 `disabled` 的条目列出但不能打开。为此 core 的 `MCPServerStdio` 加了 `cwd` |

## 参考

- [Claude Code：sub-agents](https://code.claude.com/docs/en/sub-agents)、[MCP](https://code.claude.com/docs/en/mcp)
- [Codex：advanced configuration](https://developers.openai.com/codex/config-advanced)、[subagents](https://developers.openai.com/codex/subagents)、[MCP](https://developers.openai.com/codex/mcp)
- [OpenCode：agents](https://opencode.ai/docs/agents/)、[MCP servers](https://opencode.ai/docs/mcp-servers/)
- [Cursor 论坛：Custom Modes 在 2.1 移除](https://forum.cursor.com/t/custom-modes-missing-in-cursor-2-1-0-vscode-1-105-1/143218)
- 本仓库：`docs/design/web-scopes-and-agents.md`、`docs/design/coding-features.md`、`docs/en/stability.md`
