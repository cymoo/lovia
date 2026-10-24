# 稳定性

本页说明 lovia 1.0 承诺跨版本保持不变的部分、不作承诺的部分，以及新版本仍能读取的已存数据。

## 版本策略

- **1.0 之前**，破坏性改动随补丁版本发布。
- **从 1.0 起**，重命名或移除下文“承诺范围”内的任何内容，都会保留弃用兼容层至少一个次版本。

## 承诺范围

**Python API**：以下模块 `__all__` 中的名字。

| 模块 | 说明 |
| --- | --- |
| `lovia` | 包括 `lovia.events`：流式输出产生的事件类 |
| `lovia.tools` | |
| `lovia.plugins` 和 `lovia.plugins.mcp` | |
| `lovia.workspace` | |
| `lovia.providers` | 另含用于注册[第三方 Provider](providers.md#自定义-provider) 的 `lovia.providers` entry-point 组 |
| `lovia.transcript` | Provider 和插件使用的 entry 与 delta 类型 |
| `lovia.stores` | |
| `lovia.tracing` | |
| `lovia.testing` | |
| `lovia.eval` | |
| `lovia.context` | Stage API 除外（见下文） |
| `lovia.web` | 仅 Python API |

**HTTP API**：[HTTP API](http-api.md) 中列出的路由、请求与响应结构、错误码和 SSE 帧，
`/api/config` 与 `/api/config/*` 除外。

**Web 页面**：[定制页面](web-server.md#定制页面)中列出的公开接口，即模板 block、设计变量、
`data-part` 属性、`data-i18n` key 和 `window.lovia`。

**内置工具名**：`read_file`、`shell`、`todo_write`、`recall_tool_result` 等。工具名会写进每条调用过它的
transcript，工具视图、审批规则和评测也按名字匹配，所以给内置工具改名属于破坏性改动。

**命令与环境变量**：`lovia` 和 `lovia web` 命令及其子命令和参数（见 `lovia web --help`），以及文档中
列出的环境变量：CLI 的见[常用 CLI 选项](web-ui.md#常用-cli-选项)，Provider 的见
[模型字符串](providers.md#模型字符串)和[网络](providers.md#网络超时代理tls)，另有 `TAVILY_API_KEY`、
`LOVIA_EMBEDDING_BASE_URL` / `LOVIA_EMBEDDING_API_KEY` 和 `LOVIA_EVAL_JUDGE_MODEL`。

## 不作承诺

以下内容在任何版本中都可能变化：

- `lovia.runtime`。
- `lovia.context` 的 Stage API：`Stage`、`StageContext`、各 stage 类（`OffloadToolResults`、
  `ClearToolResults`、`SummarizeHistory`）和 `Compaction(stages=...)`、`render_view`，以及提示词常量
  （`SUMMARY_SYSTEM_PROMPT`、`SUMMARY_WRAPPER`、`REQUIRED_SECTIONS`）。
- `lovia.web.config`（CLI 的模型配置）、`lovia.web.builder`，以及 `lovia.web.api` 中
  `lovia.web` 未导出的部分。
- 以 `_` 开头的名字。
- 页面的 DOM 结构、CSS 类名、元素 id 和 JavaScript 模块（包括 `api.js`）。
- `/api/config` 与 `/api/config/*` 路由：它们服务于 CLI 内置的设置界面。
- 写给模型或人看的文字：内置提示词、工具描述、压缩标记和日志消息。

## 已存数据

新版本 lovia 能读取旧版本写下的数据：

| 数据 | 兼容方式 |
| --- | --- |
| `TranscriptEntry` JSON（`entry_to_dict` / `entry_from_dict`） | 忽略未知字段；遇到未知 `type` 抛出 `ValueError` |
| SQLite 存储（session、checkpoint、Web 聊天库、记忆索引） | 每个存储的 schema 版本记录在 `lovia_schema` 表中，迁移只追加。被更新版本迁移过的数据库会被拒绝打开，而不是按错误的结构读取 |
| `Segment.meta["context_state"]` | 自带 `version`；版本不符的状态会被丢弃并重新计算 |
| `Segment.meta["timing"]` | `{"started_at", "finished_at"}`，单位为 epoch 秒；较早的 segment 没有这一项 |
| 工具输出中的 `lovia_ui` 信封 | `{"lovia_ui": {"kind", "props" 或 "from_args", "data"?}}`（见 [UI 组件](web-server.md#ui-组件)） |
| 消息 ref | `"<run_id>:<n>"` |
| 内置工具名 | 见上文 |
| `MEMORY.md` | 每行一条 `- fact`，其他行会被忽略，因此可以直接手动编辑 |
| `FileResultStore` | 每个值存为一个 `<key>.txt` 文件 |
| `.lovia/config.json`（`lovia web` 的设置） | 忽略未知字段 |
