# Web 里的 MCP 按需加载（defer）

> 状态：已实现（分支 `feat/web-mcp-defer`；第 3 版，已吸收两轮审阅）· 2026-11-27 · 范围：`lovia/plugins/mcp.py`、`lovia/web/mcp.py`、`lovia/web/config`、`lovia/web/api/config.py`、`lovia/web/static/js`。
>
> 行号指向 `main`（`64c6e37c`，v0.9.298）。

## 问题

在 Settings → MCP 里加一个 60 个工具的服务器，这 60 个工具的名称、描述和参数 schema 每次请求都会进 `tools`：每次都算输入 token，还会分散模型选工具的注意力。

core 早就有解法：`MCPServer(defer=True)`（`plugins/mcp.py:572`）。打开后，system prompt 里只列工具名，模型用 `search_mcp_tools` 查定义，再用 `call_mcp_tool` 调用。但 web 用不上这个功能，原因有两个：

- `server_for()`（`web/mcp.py:100`）不传 `defer`，`MCPServerConfig`（`web/config/schema.py:257`）也没有这个字段；
- web 的 `SettingsMCP.setup()`（`web/mcp.py:289`）是自己写的挂载流程，连不上的服务器跳过不报错，权限换成 `mcp:<tool>` scope，还会滤掉不合法的工具名。它没有经过 core 的 `MCP.setup()`，所以只改 `server_for()` 不会生效。

按需加载省的是**工具定义**，不是连接：每个 run 照样连接服务器、列出工具，工具名照样进提示词。模型查过的定义会作为工具结果留在对话记录里，之后的请求里还会带着它们；所以省下的是没用到的那些工具的定义。

## 结论

一个 PR，四处改动：

| # | 改动 | 位置 | 规模 |
| --- | --- | --- | --- |
| 1 | 把 `MCP.setup()` 里的 defer 分支提成函数 `deferred_tools()`，core 和 web 共用；同时修正 `call_mcp_tool` 的重试叠加 | `plugins/mcp.py` | 约 35 行，大部分是搬移 |
| 2 | 配置加 `defer` 字段；`SettingsMCP` 把这类服务器的工具交给 `deferred_tools()`，并检查所有服务器的工具全名有没有重复 | `web/config`、`web/mcp.py` | 约 35 行 |
| 3 | MCP 表单加「选项」区：工具定义（全部 / 按需）和系统代理（`trustEnv`），控件改写 JSON 里的键；行上加「按需加载」标注 | `mcp-config.js`、`i18n.js` | 约 70 行 |
| 4 | `call_mcp_tool` 和 `search_mcp_tools` 的内置卡片视图，审批卡片显示目标工具名 | `builtin-views.js`、`approvals.js` | 约 40 行 |

默认关闭，和 core 一致。web 不替用户决定哪些服务器按需加载。

## 1. core：`deferred_tools()` 与重试

```python
def deferred_tools(
    groups: Sequence[tuple[str | None, Sequence[Tool]]],
) -> tuple[list[Tool], str | None]:
    """(server prefix, its tools) per deferred server -> the two tools
    (search_mcp_tools, call_mcp_tool) and the system-prompt fragment naming
    every deferred tool; ([], None) when no group has a tool. Raises
    UserError on a name clash."""
```

- 函数体就是现在 `MCP.setup()` 第 942–980 行的内容：建目录、检查重名、生成 `_make_search_tool` / `_make_call_tool` / `_deferred_instructions`。`MCP.setup()` 改为调用它，行为不变。
- **空目录**：服务器连上了，但没有工具通过过滤，这时返回 `([], None)`，模型看不到这对工具。这和现在 `if deferred:`（`plugins/mcp.py:977`）的效果相同。
- **在清理保护内调用**：它可能抛 `UserError`。core 和 web 都在连接循环的 `try` 里调用它，抛错时 `except BaseException: aclose()` 会关闭已经打开的连接。放在 `try` 外面的话，`setup()` 会在交出 `PluginInstance` 之前退出，runner 拿不到 `aclose`，stdio 子进程就泄漏了。
- 「延迟工具与直接暴露的工具重名」这项检查仍留在 `MCP.setup()`，因为只有它同时知道两边有哪些工具。
- 名字不带下划线，但**不进** `plugins/mcp.py` 的 `__all__`：调用方只有两个，都在 lovia 里；进了 `__all__` 就属于 `docs/en/stability.md` 承诺的稳定 API。

**重试叠加（core 现有 bug，一并修）。** runner 调 `call_mcp_tool` 时按包装器自己的 `retries` 重试（`runtime/tool_calls.py:360`），包装器的 `retries` 是 `None`，所以用 Agent 的 `default_tool_retries`。包装器内部又用 `run_tool(target, …)` 按目标工具的 `retries` 重试一次。结果是两层重试相乘：

| 目标 `retries` | Agent 默认 | 直接调用次数 | 经包装器调用次数 |
| --- | --- | --- | --- |
| 0（web 的设定） | 1 | 1 | 2 |
| 2 | 1 | 3 | 6 |

web 设 `retries=0` 是有原因的：连接断开时服务器可能已经执行了操作，重放一次就可能执行两遍，而且绕过了审批。修法是包装器固定 `retries=0`，内部的 `run_tool` 传入 Agent 的 `default_tool_retries` / `default_tool_timeout`，让重试只在内层按目标工具的规则发生，调用次数和直接调用完全一致。

**超时不完全一致，接受。** 包装器的 `timeout` 是 `None`，所以 Agent 设了 `default_tool_timeout` 时，外层也受它限制，限的是所有尝试加退避等待的总时长。目标工具自己设了比默认值更长的超时，也会被外层截短。这只会让调用更早结束，不会多执行。web 不设 `default_tool_timeout`，不受影响。要完全一致，得让包装器表达「外层不设超时」，现在的 `Tool.timeout` 没有这个取值，不值得为此改 `Tool`。

另外，`retries=0` 只管一次执行之内的重试。run 在调用发出后被打断，从 checkpoint 恢复时会不会重放这次调用，由 checkpoint 的恢复规则决定，直接调用和经包装器调用的情况一样，不在本项范围。

## 2. web：配置与挂载

**配置**。`MCPServerConfig.defer: bool = False`。在 JSON 里写作 `"defer": true`，和 `"disabled"`、`"trustEnv"` 并列；`parse_server()` 读取（走 `_flag`），`spec_of()` 只在为 true 时写回。表单控件（第 3 节）改写的也是这个键，用户不需要知道它。

**挂载**（`SettingsMCP.setup()`，全部在现有的 `try` 内）：

```python
taken: dict[str, str] = {}  # full tool name -> the server that has it
...
usable = _usable(conn, taken)  # also drops names an earlier server took
mounted = [replace(t, permission=_permission(t.name)) for t in usable]
deferred.append((conn.prefix, mounted)) if cfg.defer else tools.extend(mounted)
...
pair, listed = deferred_tools(deferred)  # still inside try
instructions = "\n\n".join(s for s in (listed, missing) if s) or None
```

- **权限无需改动。** `call_mcp_tool` 的 permission 会委托给目标工具的 `decide()`（`plugins/mcp.py:809`），web 的 `_permission` 照常返回 `ask` 加上 `mcp:github__create_issue` scope，"完全权限"下照常放行。授权按 scope 匹配（`supervisor.py:585` 的 `has_grants`），所以通过 `call_mcp_tool` 点的"总是允许"和直接调用共用同一个授权。
- **工具全名重复。** 服务器名本身唯一，但名字里允许 `_`，拼出来的全名仍可能撞：服务器 `a` 的 `b__c` 和服务器 `a__b` 的 `c` 都是 `a__b__c`。一个直接暴露、一个按需加载时，两者会同时挂上，还共用同一个 `mcp:a__b__c` 授权。两个都直接暴露时，现在 `_collect_tools()`（`runtime/loop.py:1636`）会抛 `Tool name conflict`，这个 chat 的每个 run 都失败。
  - 处理：`setup()` 按服务器顺序（项目的在前，然后按 Settings 里的顺序）记下每个全名；后面撞名的工具不挂载，记一条 warning。
  - 为什么不报错：web 的规则是一个服务器出问题不影响整个 run（连不上会跳过，名字不合法的工具会被 `_usable` 滤掉）。撞名也按同样的方式处理，顺带修好了上面那个「每个 run 都失败」。
  - 两份清单一起检查后，`deferred_tools()` 里那项重名检查在 web 中不会触发，留着作为兜底。
  - Settings 的工具列表是**连接报告**：记的是最近一次连接时这个服务器挂上的工具。撞名丢掉了哪个，要看那次 run 里还有哪些服务器；测试按钮只连这一个服务器，不会丢。所以文档不承诺工具列表反映撞名。
  - **授权仍按全名。** 撞名的两个工具轮流胜出时（比如前一个服务器有时连不上），「总是允许」会落到当时胜出的那个上。授权按服务器身份绑定是另一个话题，见「不做」。
- **包装器名字和别的插件撞**：部署方给自定义 agent 另挂了一个开了 defer 的 core `MCP` 插件时，两边都有 `search_mcp_tools`，`_collect_tools()` 会在 run 开始时报 `Tool name conflict`。这是部署方的配置错误，报错清楚，不做特殊处理。
- **配置变了，还没跑完的调用**：被打断的 run 用当前的 agent 恢复。中途切了 defer 的话，挂起的直接调用会找不到工具，挂起的 `call_mcp_tool` 也可能找不到目标；两者都会变成一条工具错误交还模型，模型下一轮按新的工具清单重试。这和中途关掉一个服务器的现有表现一样。
- **连不上的服务器**：不会进入 `deferred_groups`，现有的 `mcp_unavailable` 通知和 system prompt 里那句说明照旧。

**项目自带的服务器（`.mcp.json`）**：文件里写 `"defer": true` 就生效。这些服务器在界面上没有切换，因为文件属于仓库，lovia 不写它。条目的 digest 覆盖整个条目，所以改了 `defer` 需要重新批准，这符合现有规则「批准的是写下来的内容」。

## 3. Settings：表单里的「选项」

只靠 JSON 键的话，用户很难知道有这个选项，手写也容易出错。`trustEnv` 现在也有同样的问题。所以 MCP 表单里所有 **lovia 自己的选项** 都用控件来设，各客户端通用的**服务器定义**（`command` / `args` / `env` / `cwd` / `url` / `headers`）继续写在 JSON 里，照服务器文档粘贴即可。

- **位置**：JSON 编辑框下面加一个「选项」区，用两个分段控件（`segmented`，模型表单的「系统代理」用的就是它）：
  - **工具定义**：`全部加载 | 按需加载`。提示文字：「按需加载：提示词里只列工具名，模型用到时再查定义。适合工具多的服务器。」
  - **系统代理**：`跟随全局 | 直连 | 经代理`，只在服务器是 `url` 时显示。文案沿用模型表单的 `cfg.proxy*`。
  - 启停（`disabled`）已经有行上的开关，不放进选项区。
- **JSON 框是唯一的真值**，和模型表单里「额外请求字段」的做法一样（`model-config.js:255`）：点控件会改写 JSON 里的那个键，选默认值就删掉这个键，保持 JSON 简洁；在 JSON 里手动改了，控件跟着变（`segmented()` 只在创建时设一次选中，所以同步要自己按值勾选对应的 radio）。所以保存、测试和接口都不用改，存的还是 `"defer"` / `"trustEnv"`。
- **解析规则与后端一致**（`config/mcp.py:41`）：外层有 `mcpServers` 就取它，否则把整个对象当作 `mcpServers`；`url` / `httpUrl` / `serverUrl` 都算 url。键的值不是布尔（比如手写了 `"yes"`）时控件显示默认，保存时由后端报错，前端不重复校验。
- **文档里正好一个服务器时才显示选项区**，编辑现有服务器时总是这样。一次粘贴多个服务器时选项区隐藏，需要的话在 JSON 里写键。JSON 不合法时控件禁用。
- **服务器那一行**：设了按需加载的，名字旁边加一个「按需加载」标注。标注只做展示，不做成控件。
- **项目视图**：从项目视图打开全局服务器的编辑表单时行为不变，改的是全局定义。按需加载是服务器本身的属性，不做按项目覆盖。

## 4. 聊天界面

不加视图的话，卡片会显示 `call_mcp_tool({"tool": "github__create_issue", "arguments": {…}})`，审批卡片会问「允许 call_mcp_tool？」，看不出调的是哪个工具。

- `call_mcp_tool` 的内置视图：summary 显示成 `github__create_issue(…)`，和直接调用的样子一致；
- `search_mcp_tools` 的内置视图：summary 显示「查找 MCP 工具：<query>」，label 用于活动分组摘要；
- `approvalHeading(name)`（`approvals.js:13`，调用处 `chat.js:1038`）改成接收调用参数，名字是 `call_mcp_tool` 时用 `args.tool` 作标题。scope chip 本来就显示 `mcp:` 后面的工具名，不用改。

## 测试

- `tests/plugins/test_mcp.py`
  - 现有 defer 测试（`test_defer_swaps_tools_for_search_and_call` 等）不改也应通过；
  - `deferred_tools()`：空分组返回 `([], None)`；重名抛 `UserError`；
  - 重试：目标 `retries=0`、Agent `default_tool_retries=1` 时，经 `call_mcp_tool` 只执行一次；目标 `retries=2` 时执行 3 次，不是 6 次；
  - 第二个服务器触发重名错误时，第一个服务器的连接被关闭（现有的 `test_setup_failure_closes_owned_connections_only` 覆盖了 `try` 内的路径）。
- `tests/web/test_mcp_settings.py`
  - `defer` 能往返解析（`parse_server` / `spec_of`），经现有的保存接口写入；只切 `defer` 不会清掉 Settings 里的上次连接状态（`_as_connected` 把它和 `enabled` 一样忽略）；
  - 一个 defer 服务器、一个普通服务器、一个连不上的 defer 服务器：tools 是普通工具加两个延迟工具，instructions 列出延迟工具名和连接失败的说明；`call_mcp_tool` 的审批拿到目标工具的 scope，完全权限下放行；
  - 唯一的 defer 服务器连不上：没有那两个延迟工具，instructions 里只有连接失败的说明；
  - 工具全名重复：后一个服务器的那个工具被丢掉。
- `tests/web/test_grants.py`（走审批接口，仿照第 217 行的现有测试）：直接调用时选「总是允许」；把服务器切成 defer 后，在同一对话经 `call_mcp_tool` 调同一个工具，断言自动放行，没有出现审批；再调同一服务器的另一个工具，断言仍然询问。光测 `decide()` 返回的 scope 测不到授权的保存和匹配。
- `tests/web/js/`：`call_mcp_tool` 的 summary 和审批标题用的是目标工具名；选项控件与 JSON 互相同步（把控件设成默认值会删掉键，JSON 里改了键控件跟着变，多个服务器时选项区隐藏）。这些逻辑写成纯函数，DOM 只负责接线。

## 文档

- `docs/{en,zh}/web-ui.md` 的 MCP 小节（zh 第 209 行那段字段说明）：说明表单里的「选项」区（工具定义、系统代理）和对应的 `"defer"` / `"trustEnv"` 键；说明每个 run 仍会连接服务器并列出工具，按需提供的是工具定义；说明工具全名重复时后来的那个会被丢掉。
- `README.md` 和 `README-zh.md`：在 lovia web 的设置说明里各补一句「工具多的 MCP 服务器可以设为按需加载」。

## 不做

| 项 | 原因 |
| --- | --- |
| 超过阈值自动按需加载 | 替用户做决定，而且行为会随服务器的工具数悄悄改变；core 默认是关闭 |
| 工具数多时在 Settings 里提示 | 先不做，等有人用了再看 |
| 行上直接放切换、另开 `PUT .../defer` 接口 | 选项区已经能设，行上再放就是第二个入口；也免得多一个接口 |
| `cwd` / `env` / `headers` 也做成控件 | 它们是各客户端通用的服务器定义，照服务器文档粘贴；只有 lovia 自己的选项才需要发现性 |
| 按项目覆盖 defer | 它是服务器的属性，不是各项目的选择；项目自带的服务器在 `.mcp.json` 里自己写 |
| 按工具启用/停用（`includeTools` / `excludeTools`） | 和本项无关；core 已经支持，需要时单独加 |
| 语义/向量检索 | 要引入 embedding 依赖；工具名一直在提示词里，模型多半直接按准确名字查 |
| 部署方用 `registerToolView('github__x')` 注册的视图对 `call_mcp_tool` 也生效 | 要在视图层把调用转给目标工具，复杂度不值；按需加载的服务器显示通用卡片 |
| web 联网搜索（`web/search.py`）的服务器也按需加载 | 它只暴露一个工具 |
| 限制服务器名里出现 `__` | 现有配置可能已经这样写；而且只靠 `_` 也能拼出重名（`a` + `_b` 和 `a_` + `b`），挡不住 |
| 授权按服务器身份（而不是工具全名）绑定 | 撞名本身就极少见（要靠名字里的 `_` 拼出来）；项目服务器顶替同名全局服务器时共用授权也是现有行为，和 defer 无关。要做就得改授权的存储格式和每一处匹配，单独设计 |
| 恢复挂起的审批前核对目标仍是原来那个服务器 | 同上：直接调用恢复时也按名字找工具，是现有行为；要触发得同时满足「run 挂在审批上」「项目的 `.mcp.json` 条目被改成待批准」「另一个服务器恰好有同名工具」 |
| `Tool.timeout` 加「不设超时」的取值，让包装器的超时和直接调用完全一致 | 只在 Agent 设了 `default_tool_timeout` 时才有差别，而且只会更早结束；web 不设 |
