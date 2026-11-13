# 文件面板：从"最近"到"变更"，以及 diff

> 状态：已实现（v0.9.186，两期一起落地）· 2026-11-13 · 范围：`lovia/web`（`api/workspace.py`、`workspaces.py`、`static/js/files.js`、`chat.js`）。core 与 workspace 引擎不改。
>
> 行号指向 `main`（`38ad6556`，v0.9.185）。

## 结论

文件面板的第一个标签现在叫"最近"，它列的是"磁盘上最近变过什么"。用户想知道的是"助手改了什么、改成了什么样"。

改法是两件事：

1. **列表只列改动**。标签改名"变更"，分两组：
   - **本对话**：助手在本对话里写过、改过的文件，以及用户上传的文件。
   - **其他未提交改动**：只在 git 仓库里出现，默认折叠，来自 `git status`。
2. **查看器能看 diff**。git 仓库里，已跟踪且有改动的文件多一个"改动 / 文件"切换，内容来自 `git diff HEAD`。

不引入存储，不改 session 协议，不新增依赖。非 git 目录只有"本对话"一组，没有 diff；严格的"本对话改了什么"和还原留给 #146。

## 现状

| 问题 | 原因 |
| --- | --- |
| 新工作区没写任何文件，列表就有一大堆 | `/api/workspace/recent` 遍历整棵树后按 mtime 排序取前 100（`api/workspace.py:356`） |
| 大仓库里"最新"也不准 | 遍历在 `max_list_results`（500）处截断，然后才排序 |
| "本对话"分组可能漏文件 | 前端只在返回的 100 项里找触碰过的路径（`files.js:712`）；不在这 100 项里就不显示 |
| 历史回放把失败的写入也算进去 | 实时路径检查了 `is_error`（`chat.js:2753`），回放路径只看工具调用、不看结果（`chat.js:2329`） |
| 没有 diff | 只有工具卡片里 `edit_file` 的 old/new；`write_file` 覆盖写和 shell 改动都看不到 |

## 设计

### 1. 列表

**本对话**这一组不再依赖遍历。前端把触碰过的路径交给服务端，服务端逐个 `stat`：

- 开销与触碰的文件数成正比，与仓库大小无关。
- `stat` 失败的路径（已删除、被拒绝、在根目录之外）不列。
- 回放路径补上成功判定，与实时路径一致。
- 排序沿用 mtime 倒序，scratch 目录下的文件照旧不列。
- 空对话显示空状态和上传入口；全量文件留给"浏览"标签。

**其他未提交改动**这一组来自 git：

- 已经在"本对话"里的路径不重复出现。
- 默认折叠，标题带数量，例如"其他未提交改动 · 7"。
- 未跟踪的目录作为一行显示，点击进入"浏览"。目录里有"本对话"的文件时展开（多跑一次 `--untracked-files=all`），只列其余文件，免得同一个文件在两组里各出现一次；像 `uploads/` 这样只装着本对话文件的目录就不再出现。
- 超过 200 项截断，末尾显示"还有 N 项"。

两组里的每一行，在 git 仓库中都带状态和行数：

| 状态 | 显示 |
| --- | --- |
| 已修改 | `+12 −3` |
| 未跟踪 | "新文件"标记 |
| 已删除 | 路径加删除线，`−40` |
| 无改动或被忽略 | 不加标记 |

### 2. Diff 查看器

- 已跟踪且有改动的文件，查看器顶部出现"改动 / 文件"两个标签（沿用文件面板现有的标签条样式）。
- 从"变更"列表打开时默认停在"改动"，从"浏览"打开时默认停在"文件"。
- 采用 unified 视图：行号槽加增删着色，复用现有的 `lv-diff-old` / `lv-diff-new`。首版不做并排视图，也不在 diff 里做语法高亮。
- 标签旁注明基线："相对上次提交"。
- 未跟踪文件不提供切换：全绿的 diff 没有信息量，直接显示文件。
- 二进制文件显示"二进制文件有改动"。
- diff 超过输出上限（`max_shell_output_chars`，30 000 字符）时截断，并提示切到"文件"查看全文。
- 助手再次写入正在查看的文件时，diff 随现有的 `maybeReloadViewing` 一起刷新。

### 3. git 检测与执行

检测和查询都在**文件所在的主机**上、通过 workspace session 执行，本地、SSH、Docker 走同一条路。

`open_view()` 加一个 `git=True` 参数，在 readonly 策略上放行三条固定前缀：`git rev-parse`、`git status`、`git diff`。其余 shell 命令照旧被拒。已在本仓库验证：

| 命令 | 结果 |
| --- | --- |
| `git status --porcelain=v1 -z -- .` | 放行，正常执行 |
| `git diff --no-ext-diff --no-textconv HEAD -- <path>` | 放行，正常执行 |
| `git log -1` | 拒绝 |
| `git status; rm -rf x` | 拒绝（复合命令逐段判定） |
| `git diff HEAD --output=/tmp/x` | 拒绝（路径守卫拦下根目录之外的路径） |

用到的命令：

| 用途 | 命令 | 说明 |
| --- | --- | --- |
| 检测 | `git rev-parse --show-prefix` | 退出码 0 即可用；输出是工作区在仓库里的前缀，用来把 status 的仓库相对路径换算成工作区相对路径 |
| 列表 | `git status --porcelain=v1 -z --untracked-files=normal -- .` | `-- .` 把范围限定在工作区内；`normal` 让未跟踪目录折成一行 |
| 行数 | `git diff --numstat -z HEAD -- .` | 一次取回所有已跟踪文件的增删行数 |
| 单文件 | `git diff --no-ext-diff --no-textconv HEAD -- <path>` | `<path>` 由服务端 `shlex.quote`，放在 `--` 之后 |

约定：

- 所有命令带 `GIT_OPTIONAL_LOCKS=0`，面板的只读查询不与助手正在跑的 git 抢 index 锁。
- `--no-ext-diff --no-textconv` 是为了拿到稳定的 unified 文本，不是安全措施。
- 不关 `core.fsmonitor`，也不设法屏蔽 clean filter。它们的命令来自用户自己的 git 配置，用户敲 `git status` 时同样会执行。
- 检测失败一律视为"不是 git 工作区"：没装 git、不在仓库里、仓库归属不受信任都不报错，面板退回只有"本对话"一组。
- 仓库还没有任何提交时 `HEAD` 不存在，`numstat` 和单文件 diff 会失败；此时所有文件按未跟踪处理。
- git 返回的路径在回给页面前，过一遍 session 的读权限判定，`denied_paths` 隐藏的文件不出现。

### 4. 接口

`/api/workspace/recent` 删除，换成两个接口：

**`POST /api/workspace/changes`**

```jsonc
// 请求
{ "paths": ["report.md", "src/app.py"] }   // 本对话触碰过的路径，最多 500

// 响应
{
  "git": true,
  "truncated": false,
  "files": [
    { "path": "src/app.py", "touched": true,  "status": "modified",  "added": 12, "removed": 3, "size": 2048, "mtime": 1763000000.0 },
    { "path": "report.md",  "touched": true,  "status": "untracked", "size": 512, "mtime": 1763000100.0 },
    { "path": "docs/old.md", "touched": false, "status": "deleted",  "removed": 40 }
  ]
}
```

`status` 取 `modified` / `untracked` / `deleted` / `renamed`，无改动时为 `null`。`git: false` 时只有 `touched` 的条目，且不带 `status`。

**`GET /api/workspace/diff?path=`**

```jsonc
{ "path": "src/app.py", "diff": "@@ -1,4 +1,6 @@ …", "binary": false, "truncated": false }
```

两个接口都沿用现有的 `agent` / `session` / `root` 作用域参数。

### 5. 性能

- 面板关闭时不发任何请求。
- 打开面板、手动刷新、助手写入文件后（现有的防抖刷新）各触发一次 `changes`：一个 session、最多三条 git 命令。
- shell 运行后仍只点亮刷新按钮（现有的 `workspace-maybe-stale`），不自动重跑 status。
- 单文件 diff 只在打开文件时请求。
- 检测结果不缓存：`rev-parse` 只要几毫秒，而用户可能随时 `git init`。

## 分期

| 期 | 内容 | 依赖 |
| --- | --- | --- |
| 1 | 列表改为按路径 `stat`；标签改名；回放补成功判定；删除 `/recent` 的全树遍历 | 无，纯 web 层 |
| 2 | `open_view(git=True)`；`changes` 带 git 状态和行数；"其他未提交改动"分组；diff 查看器 | 第 1 期 |

第 1 期单独就能解决"新工作区一大堆文件"。#362（回合末尾的"改动文件"卡片）可以在第 2 期之后直接复用 `changes` 的行数。

## 不做

| 方案 | 理由 |
| --- | --- |
| shadow git（Cline、Gemini CLI、OpenCode 的做法） | 同样要求主机装 git；每步快照在大仓库里慢；还要处理存放位置和 GC |
| 写前镜像与还原 | 属于 #146。它需要存储、GC 和写路径上的钩子，是唯一能给出严格"本对话改动"的机制，值得单独设计 |
| 独立的"Git 变更"标签页，区分 staged / unstaged | lovia 不是 git 客户端；折叠的一组已经够用 |
| 区分"没装 git""不受信任""不是仓库"并分别提示 | 对用户的结果相同：没有 diff |
| 屏蔽 fsmonitor、clean filter | 风险来自用户自己的 git 配置，不是面板引入的 |
| 自己用 `difflib` 算 diff | git 已经处理了二进制、重命名和换行差异 |
| 并排 diff、diff 内语法高亮 | 首版先把 unified 做好 |

## 实现时的决定

1. **标签名**：叫"变更"，第二组标题"其他未提交改动"。
2. **基线语义**："相对上次提交"，界面如实标注，不做归因。
3. **`/api/workspace/recent`**：在 `http-api.md` 里，属于承诺范围；按 1.0 前的规则在补丁版本里直接移除，文档同步。
4. **上传的文件**：消息附件在渲染用户消息时（实时和回放）进入 `touched`；面板上传在上传成功时进入，但它不在任何消息里，换对话或刷新后就不再列在"本对话"。
5. **与草案的出入**：`status` 多一个 `added`（已暂存的新文件，按新文件显示）；`changes` 回 `truncated` 而不是"还有 N 项"（输出被截时数不出 N）；diff 接口多一个 `old`，重命名按一次改名比较；git 命令另设 `GIT_LITERAL_PATHSPECS=1`，路径不当 glob。
