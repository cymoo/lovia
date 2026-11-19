# 消息附件：从工作区的 `uploads/` 移到对话自己的目录

> 状态：第 1 步已实现（v0.9.239），第 2 步已实现（v0.9.240）；第 3 步待做 · 2026-11-19 · 范围：`lovia/web`（`store.py`、`attachments.py`、`api/workspace.py`、`api/chat.py`、`api/deps.py`、`static/js/chat.js`、`files.js`），远程部分加两个 workspace 引擎操作（`workspace/remote/serve.py`）。core 不改。
>
> 行号指向 `main`（`b91ae5bb`，v0.9.238）。

## 结论

输入框附件现在写进工作区根目录的 `uploads/`。附件是**对话的输入**，不是项目的内容，所以改成归对话所有：

1. **位置**：`<聊天数据库所在目录>/attachments/<attachments_id>/<文件名>`。CLI 下是 `~/.lovia/attachments/`，lovia-multiuser 下是每个用户自己的 `users/<id>/attachments/`。不加配置项。
2. **归属**：对话表加一列 `attachments_id`。新对话是自己的 id；fork 出来的对话和子任务继承来源的值，共用一个目录。
3. **生命周期**：删除对话时，如果已经没有对话用这个 `attachments_id`，就删掉目录。启动时清理一次没人认领的目录。
4. **agent 访问**：附注里给出绝对路径，并给这次 run 的策略加两条规则：放行读这个目录；在基线里禁止写它，所以任何权限模式（包括 trusted）都改不了附件。部署方的 `denied_paths` 优先。能看图的模型照旧直接收到图片。
5. **文件面板去掉上传**，面板变成只读；`@路径` 不变。
6. **远程工作区**：在主机的临时目录里放一份副本，由主机上的引擎自己定位置并安全创建；不在主机上删除。

## 现状

| 问题 | 原因 |
| --- | --- |
| git 仓库里多出未跟踪的 `uploads/`，`git add -A` 会把截图、表格一起提交 | 上传写到 `<root>/uploads/`（`api/workspace.py:201`、`_write_upload` `:255`） |
| 和项目自己的 `uploads/` 混在一起（Django media、Express 等常用这个名字） | 目录名固定 |
| 项目工具会扫到附件：pytest 收集上传的 `test_*.py`，`docker build` 的 `COPY . .` 会带上它们 | 附件就在项目树里 |
| 删除聊天不删附件（文档明说"需要由应用或用户清理"） | 文件和对话没有关联 |
| 没有工作区的对话不能发附件 | 附件按钮跟着工作区显示（`chat.js:4177`） |
| 上传能绕过对话的只读模式 | `open_view(write=True)` 是网页端唯一的写入例外（`workspaces.py:108`） |
| 文件面板的"上传"和输入框附件写到同一个地方，两种意图混在一起 | 共用 `/api/workspace/upload`（`files.js:1803`） |

## 设计

### 1. 位置与归属

- `ChatStore` 持有附件根目录：`ChatStore.sqlite(path)` 取 `Path(path).parent / "attachments"`；`ChatStore(session, meta_path=…)` 从 `meta_path` 推出，也可以传 `attachments_dir=` 覆盖；`meta_path=":memory:"` 用临时目录（测试）。`create_app` 不加参数。
- `chat_sessions` 加一列 `attachments_id TEXT NULL`，并建索引。旧对话是 NULL，按 `COALESCE(attachments_id, id)` 计算，不用回填。
- 赋值规则：

  | 对话怎么来的 | `attachments_id` |
  | --- | --- |
  | 新对话、计划任务触发的对话 | 自己的 id |
  | fork（整段或截断） | 来源的值（在 `_publish` 的同一个事务里复制，`store.py:960`） |
  | 子任务 | 父对话的值 |

- **为什么不按对话 id 分目录**：附件的绝对路径会写进对话记录，不只在附注里，也在工具调用参数（`read_file(path=…)`）和 agent 的回复里。`fork` 原样复制对话记录，所以路径必须在整组 fork 里都有效。让它们共用一个目录，就不用复制文件，也不用改写对话记录。
- **为什么不按内容命名、用引用表**：这样做也能解决 fork 的问题，但要多出引用表、逐个文件的放行规则，steer 时还要 `retune`。一列就够了。

### 2. 上传

- `POST /api/sessions/{id}/attachments`（multipart），替换 `POST /api/workspace/upload`。
  - `id` 必须是 32 位小写 hex，否则 422。这个 id 会成为目录名，而 `session_id` 是客户端传的任意字符串。
  - id 是已有对话：写进它的 `COALESCE(attachments_id, id)` 目录。
  - id 还不存在：这是一个**草稿**，写进 `attachments/<id>/`。客户端在第一次上传时生成草稿 id，发送时把它当作 `session_id`。服务器本来就会用客户端传入的未知 `session_id` 新建对话（`api/chat.py:164`），所以对话建好时目录已经在正确的位置，不需要暂存区，也不需要移动文件。
- 写入沿用现在的规则：`_safe_upload_name` 处理文件名，扩展名白名单和大小上限照旧（`LOVIA_UPLOAD_ALLOWED_EXT`、`LOVIA_MAX_UPLOAD_MB`），先 `stat` 探测再用 `create_only` 独占写入，重名就加后缀（`a.png` → `a-1.png`）。大小写不敏感的文件系统上，`A.png` 和 `a.png` 也会因为独占写入失败而换后缀。
- 返回 `{ name, mime, kind, size }`，**不返回路径**，页面始终看不到服务器的目录结构。
- `ChatRequest.attachments[]` 和 steer 只传 `name`，由服务器在这个对话的目录里拼出路径。客户端的路径一律不信，这和现在 `user_content` 的原则一样，只是更直接。

### 3. 交给模型

- 图片照旧：模型能看图时作为 `ImagePart` 直接发给模型（`attachments.py:39`）。
- 附注改成每行一个路径，不再用 `", "` 分隔。数据目录本身可能带逗号，比如 `C:\Users\Li, Wei\`：

  ```
  [Attached:
  /Users/me/.lovia/attachments/3f…a1/screenshot.png
  /Users/me/.lovia/attachments/3f…a1/sales.xlsx
  Read with your file tools when relevant.]
  ```

  没有工作区的对话省掉最后一行。前端 `splitAttachmentNote`（`chat.js:4463`）同时认新旧两种格式：旧的 `[Attached in the workspace: …]` 继续走 `/api/workspace/raw`，新的走 §5 的接口；取文件名改成按 `/` 和 `\` 都切。
- `describe_image` 的描述和参数说明（`vision.py:73`、`:84`）去掉 `uploads/`，改为"用户附件的路径见消息附注"。

### 4. agent 的权限：能读，不能写

web 层组装每个对话的运行策略时加两条规则，加在 `deps.at_root` 和运行绑定同一处；子任务通过继承来的 `attachments_id` 拿到同样的两条。绝对路径的规则本身就覆盖整个子树，所以规则写目录本身，不写 `/**`。

| 规则 | 放在哪 | 为什么放这里 |
| --- | --- | --- |
| `PathRule("<附件目录>", "allow", ops={"read"})` | `path_rules` 最前面 | 放行只能写在 `path_rules` 里（基线只能收紧）；放最前面，是因为 `path_rules` 按第一条命中生效 |
| `PathRule("<附件目录>", "deny", ops={"write"}, reason="Attachments are read-only; copy one into the workspace to change it.")` | 基线（`replace(policy, baseline=(*原基线, 规则))`） | 只放行读的话，写入不命中任何规则，会落到默认的 `write_outside`，而 `trusted()` 的 `write_outside` 是 `allow`（`workspace/policy.py:738`），trusted 对话就能覆盖附件。基线里所有命中的规则会合并、取最严格的那个，`path_rules` 推翻不了，所以任何权限模式下都禁止写 |

- 放行的是**目录**，不是逐个文件，所以 steer 中途新增的附件自然在范围内，不需要 `retune`。
- 部署方的 `denied_paths` 仍然优先：部署方明确禁止了，就以部署方为准，这时图片照样直接发给模型，只是工具读不了。
- 附件对 agent 是不可改的原始输入。想放进项目时，由 agent 复制到工作区，按对话的权限模式审批。
- 能保证到什么程度：写入禁止能拦住文件工具（`write_file`、`edit_file`）和 shell 的重定向。但 shell 命令的检查只看字面，`cp x <附件目录>/y` 里的目标路径会被当作读取参数放行，任意程序也能直接写。这和所有路径规则在本地后端的限制一样（`workspace/policy.py` 开头的说明），真正的隔离要靠沙箱。远程副本被这样改坏时，由 §7 按大小重新放一份来恢复。

### 5. 页面

- `GET /api/sessions/{id}/attachments/{name}`：用这个对话的 `attachments_id` 定位目录，文件名只接受单个文件名（不能带路径）。MIME 规则和 `download=1` 与 `/api/workspace/raw` 一致（SVG 以下载形式返回）。草稿 id 也能读，供输入框显示缩略图。
- fork 出来的对话自然能显示来源对话的附件；对话删除后接口返回 404。
- "变更"标签不再列附件，它们已经不在工作区里了。

### 6. 生命周期

| 场景 | 处理 |
| --- | --- |
| 发送 / steer | 附件已经在目录里，什么都不用做 |
| fork | 复制 `attachments_id`，磁盘上不动 |
| rewind、截断 fork 丢掉的消息 | 不做处理，附件留到这一组对话全部删除 |
| 归档 | 不动 |
| 删除对话（`store.py:1096`，子任务一并删除） | 在删除元数据的**同一个事务**里查还有没有对话用这个 `attachments_id`；事务提交后，如果已经没有，就尽力删掉目录（`rmtree`，忽略错误） |
| `delete_all`（`:1143`） | 尽力删掉整个附件根目录 |
| 没发出去的草稿、崩溃残留、Windows 上删除失败的目录 | 启动时清理一次：没有任何对话认领、且修改时间超过 24 小时的目录 |

并发：fork 在 `_publish` 里会重新检查来源是否还在（`store.py:1002`），而 SQLite 的写事务是串行的。所以删除事务里的检查，要么看到了 fork，就保留目录；要么 fork 因为来源已删而失败。不会出现"目录删了，fork 还在用"的情况。

### 7. 远程工作区

服务器上那份是唯一的原件，主机上的只是缓存。

- workspace 引擎加两个操作，都只用标准库：
  - `attachments_list(id) -> [(name, size)]`；
  - `attachments_put(id, name, data)`：在引擎自己定的附件根目录下写入，存在就覆盖，并校验 `id` 是 hex、`name` 是单个文件名。
- 附件根目录由主机上的引擎计算，不由服务器写死：

  | 主机系统 | 位置 |
  | --- | --- |
  | Windows | `%TEMP%\lovia\attachments\`（`tempfile.gettempdir()`，每个用户独立） |
  | macOS | `$TMPDIR/lovia/attachments/`（每个用户独立） |
  | Linux / 其他 POSIX | `/tmp/lovia-<uid>/attachments/` |

  在 POSIX 系统上，`lovia-<uid>` 要安全地创建：`mkdir` 时就设 0700；如果目录已存在，用 `lstat` 确认不是符号链接、属主是当前用户、权限是 0700，否则拒绝写入。不这样做，共用 `/tmp` 的其他用户可以抢先建好这个目录，让附件写到他能读的地方。
- **什么时候放到主机上**：发送和 steer 时写过去。对于服务器上附件目录不为空的远程对话，每次 run 开始前再 `attachments_list` 一次，缺的文件和大小对不上的文件都重新放一份，这样容器重建、临时目录被清、副本被改坏之后都能自动恢复。没有附件的对话不多花一次往返。
- 附注和放行规则里用的是主机上的路径。
- **不在主机上删除**，交给系统清理临时目录，Codex 和 opencode 也是这样。服务器上的那份照常随对话删除，文档里要写明这一点区别。
- 主机连不上：工具本来就跑不了，和现在一样返回 502。主机写不了（只读容器）：图片照样直接发给模型，附注写明"文件在主机上不可用"。

### 8. Windows

- `_safe_upload_name`（`api/workspace.py:243`）补上 Windows 保留设备名：`con`、`prn`、`aux`、`nul`、`com1`–`com9`、`lpt1`–`lpt9`，不区分大小写，带不带扩展名都算，遇到时在前面加 `_`。这个问题现在的代码就有。
- 路径长度：`C:\Users\<u>\.lovia\attachments\<32位hex>\<最长120字符>` 在 260 字符的 MAX_PATH 以内。
- 删除：文件被打开时删不掉（agent 正在读、接口正在传输），所以只尽力删，删不掉的由启动清理重试，删除对话本身不因此失败。
- 附注里的反斜杠路径：按行分隔，取文件名时按 `/` 和 `\` 都切（§3）。

### 9. 去掉文件面板上传

- 删掉面板的上传按钮、拖放处理和 `uploadToWorkspace`（`files.js:1803`），删掉 `/api/workspace/upload` 和 `open_view` 的 `write` 参数。网页端对工作区只剩只读访问。
- 理由：
  - 本地工作区用系统的文件管理器放文件更好用；
  - 远程场景用"发附件，再让 agent 存进项目"可以覆盖，写入按对话的权限模式审批；
  - 留着上传，面板会被要求变成文件管理器（删除、重命名、建文件夹）。
- 以后真有需求，再在附件卡片上加"保存到工作区…"。

## 不做和否决

| 方案 | 为什么不做 |
| --- | --- |
| 工作区里放自己忽略自己的目录（`.lovia/uploads/` 或 `uploads/.gitignore`，内容为 `*`，LibreChat 的做法） | `.gitignore` 只管得住 git，docker 构建上下文、打包、`rsync`、IDE 索引还是会碰到；`git clean -fdx` 会删掉还在用的附件；删对话时要去删用户仓库里的文件；只读工作区写不进去；`.lovia` 属于 `PROTECTED_CONFIG` |
| 改用户的 `.gitignore` 或 `.git/info/exclude` | 动用户的文件或 git 内部状态，worktree 和子模块也不好处理 |
| 判断是不是 git 仓库再决定放哪 | 仓库后来 `git init` 了，位置就变了 |
| 一个全局目录（不管 store 是哪个） | 多用户时附件混在一起；删除用户目录时漏掉附件 |
| 按内容命名 + 引用表 | 一列 `attachments_id` 就能解决 fork，见 §1 |
| 以 BLOB 存进数据库 | 工具需要磁盘上的文件，等于存两份；数据库会迅速变大 |
| `create_app(attachments_dir=)` | 位置能从数据库路径推出；需要时在 `ChatStore` 上覆盖 |
| 截断 fork 时只带被保留消息的附件、rewind 时删附件 | 需要按 run 记录附件，收益只是早一点删除 |
| 远程主机上的副本随对话删除（对账或追踪表） | 需要引擎删除操作和命名空间；交给临时目录清理即可 |

暂不处理：上传大小和类型限制是进程级的环境变量，multiuser 没法按用户设置。等构建者提出需求再改成 `create_app` 选项。

## 接受的代价

1. 同一组 fork 出来的对话共用目录，一个分支能读到另一个分支后来上传的附件。它们出自同一段对话、属于同一个用户、绑定同一个目录（对话绑定的目录一旦设定就不会变）。
2. rewind 或截断 fork 丢掉的消息，附件会一直留到这一组对话全部删除。
3. 远程主机上的副本不随对话删除。
4. 移动数据目录后，agent 重新读旧附件的绝对路径会失败；页面预览和已经发给模型的图片不受影响。
5. 用户在项目目录里找不到附件，要找时可以从消息里的附件卡片打开或下载。

## 和其他 harness 的对照

（2026-11 调研，开源项目以源码为准，闭源产品以官方文档为准。）

- **放在仓库外**是主流做法：Claude Code 按会话放在临时目录，它的 Remote Control 把网页或手机发来的附件放在 `~/.claude/uploads/<session>/`；Codex 放在系统临时目录；Gemini CLI 放在 `~/.gemini/tmp/<project>/images/`，源码注释明确说是为了放在用户项目目录之外。调研的 harness 里，只有 OpenHands 直接写进工作区。
- **图片直接发给模型，同时给出路径**：Claude Code、Codex 都是这样；Claude Code 读粘贴的图片不弹审批，Gemini 对自己的临时目录读写都放行。
- **我们不同的地方**：
  - 浏览器拿不到文件的原路径，所以必须留一份副本；xlsx、pdf 这类文件在恢复对话后还要被工具重新读，所以副本放在持久的数据目录，不放临时目录。
  - 有明确的删除对话操作，又有多用户部署，所以附件随对话删除，比 Claude Code 的 30 天清理和 Codex 的从不删除都严。
- **远程**：参照 opencode，服务端在远程时把文件写到服务端的临时目录，再给出那里的路径。

## 分步

每一步都同步中英文 README 和 `docs/{en,zh}/web-ui.md`、`http-api.md`。

1. **本地附件归对话**：§1–§6、§8、§9。包括数据库迁移、新接口、附注格式、读放行和写禁止两条规则、删除与启动清理、前端解析新旧两种附注、去掉面板上传、补 Windows 保留名。这一步里远程对话的附件仍然按旧方式写进主机的工作区，第 2 步再改。
2. **远程副本**：§7，包括两个引擎操作、主机上安全创建目录、run 开始前补齐。`tests/workspace/test_engine_stdlib.py` 要继续通过。
3. **没有工作区也能发图片**：附件按钮不再依赖工作区，没有工作区时只接受图片。

## 测试要点

- fork（整段和截断）后，原对话删除，fork 里的附件仍然能预览，agent 也仍然能读；整组都删除后目录消失。
- 删除和 fork 并发时，不会出现目录被删、fork 还在用的情况。
- 子任务能读父对话的附件；附件目录之外的其他路径仍然按原规则判断。
- 草稿：先上传再发送，对话建好时附件已在正确目录；没发出去的草稿在启动清理时被删除，前提是超过 24 小时。
- `session_id` 或文件名带 `..`、`/`、`\`，或者不是 hex：都被拒绝。
- `denied_paths` 盖住附件目录时，工具读不了，但图片照样直接发给模型。
- 在每种权限模式下（含 `trusted`），`write_file`、`edit_file` 和 shell 重定向（`> <附件目录>/x`）写附件目录都会被拒绝；即使部署方在 `path_rules` 里放行写这个目录，也还是被拒绝。
- 远程：副本大小被改动后，下一次 run 开始前会重新放一份。
- 旧消息里 `[Attached in the workspace: uploads/…]` 照样显示。
- 远程：主机的临时目录被清空后，下一次 run 自动补齐；`/tmp/lovia-<uid>` 已被别人建成符号链接或属主不对时，拒绝写入。
- Windows：上传 `con.png` 会存成 `_con.png`；删除时文件被占用，删除对话仍然成功，启动清理会收掉残留目录。
