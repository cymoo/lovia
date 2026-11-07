# 权限机制重构：少问、问得值、拦住最危险的

> 状态：v2.3 评审通过，PR 1（core：说清楚，v0.9.134）与 PR 2（web：记住批准，v0.9.135）已合入；PR 3（workspace 默认值，v0.9.136）已实现 · 2026-11-07 · 范围：core（`tools/base.py`、`approvals.py`、`events.py`、`runtime/tool_calls.py`）、`workspace/policy.py`、`lovia/web`（审批卡片、授权）
>
> 参考：[kits 的 Agent 权限方案调研](https://github.com/cymoo/kits/blob/main/docs/references/agent-permissions.md)。它调研了 OpenCode / Claude Code / Codex / Cursor / Gemini CLI 的做法并做了实测，结论是“把问人从默认路径改成最后一道防线”。本文取其结论，按 lovia 的结构落地：lovia 不是 OpenCode 的宿主，权限判定就在自己的 preflight 里，没有内核可绕、没有事件竞态，所以方案比 kits 更直接。
>
> v2 修订（2026-11-07，一次外部评审之后）：baseline 从“排最前的规则”改为“与普通判定取最严格”；命令规则加“前缀 + 必含旗标”语义并剥掉前导赋值和包装命令；绿名单收窄并拆成可替换的规则包；scope 的生成改为规则声明 + 灰区首词；授权存独立表并补齐生命周期契约；`Verdict` 与 `Approval` 分清；分期改为 core → web → workspace 默认值。订正了两处对现状的误述（web 的 always 不盖 deny；断线不拒绝）。
>
> v2.1（同日）：`.env*` 从 baseline 移到预设层，一行规则就能放开；baseline 拆成三个具名包；命令词匹配改为全平台大小写不敏感，`cmd.exe` 的 `/x` 开关算旗标，规则包补 Windows 条目，顺手修 `dir /b` 被当成路径的误报。
>
> v2.2（同日，第二轮评审）：撤回“必含词任意位置”的通用语义（`python -m pip install pytest` 会命中 `python -m pytest`），`CommandRule` 仍是纯前缀，只加一个显式的 `flags=`；剥赋值 / 包装后的结果最多到 `ask`；灰区命令不再自动生成 scope，范围只来自显式规则；授权记录带工作区上下文，写入先于唤醒；`permission` 谓词不再接受 bool。
>
> v3（2026-11-08，PR 3 四轮评审之后退一步）：四轮 9 条发现归为两个架构问题——放行命令让 advisory 的词法守卫承担了写判定（`-o x`、`-ox`、`--output=` 拼写永远补不完），以及预设的保护规则放在了能覆盖模式默认值的那一层（`.env*` 两次降低 readonly 的限制）。改法：守卫只做读声明和重定向写声明，带输出样子选项的段**封顶到 ask**（与剥包装同构，不解析路径）；**所有保护进 baseline**（含 `ENV_FILES`），用户层只放 allow/ask 包；instructions 只从顺序无关的 baseline 和模式默认值推导，删掉遮蔽启发式。放开 `.env` 的写法变为 `baseline=DANGEROUS_COMMANDS + CREDENTIAL_PATHS + PROTECTED_CONFIG`。第五、六轮把封顶原则补完整为“**allow 只覆盖字面参数**”：policy 按 shell 语义分词（去引号、`#` 不当注释），短选项组含 `o` 即输出选项，通配符 / 变量 / 命令替换一律封顶；自己遍历目录树的读取工具（`grep -r`、`rg`、`diff -r`、`findstr /s`）移出 `READ_ONLY`——它们不点名就读到 `.env` 和 `id_rsa`，逐文件判定的搜索是 `grep_files`。第七轮（`git diff --no-index`、`sort --files0-from`、`wc`、cmd 的 `^`/`%VAR%`、数字文件名）把这条推到了终点：**直接放行的命令必须打印不出文件内容**。每个内容读取器都有自己不点名读文件的办法，词法守卫永远追不上；元数据类（`ls`、`du`、`stat`、`tree`、`git status/log`）留下，内容类（`cat`、`head`、`tail`、`sort`、`cut`、`wc`、`file`、`type`、`more`、`fc`）回到灰区，`read_file` 才是读取器。这样守卫只对它能做对的两件事负责：重定向写入，和字面路径的拒绝。第九、十轮是局部修正，不动原则：重定向前的数字一律当读声明（分词后分不清 `2>&1` 和 `cat 123 > out`）；带选项的包装词和含展开的命令词不给授权范围（它们不是实际运行的程序）；`git diff` 移出 `READ_ONLY`（非仓库目录下隐式 `--no-index`，会打印未跟踪内容）；baseline 内部由首个匹配改为**全部匹配取最严**（规则包怎么拼都不互相遮蔽，与"只会加严"的承诺一致）。第十一、十二轮暴露了这几轮共同踩的一条缝：规则匹配和授权范围都把"段的第一个词"当程序名，而第一个词不是程序的情况有三类——shell 语法（`!`、`(`、`{`、`if`/`while`/`do`、前置重定向，**封闭集合**：POSIX 语法）、包装程序（`command`/`exec`/`builtin`/`env`/`nice`…，小的约定列表）、解释器（`bash -c`、`xargs`，文档已承认不管）。此前只处理了第二类。现在策略分词与守卫一致（标点成独立 token），先跨过第一类再剥第二类：语法不改变运行的程序，跨过后内层判定原样成立（`(ls)` 就是 `ls`，`! git push --force` 撞上 deny）；包装词仍封顶、去范围。至此这条缝只剩"下一个包装程序的名字"。
>
> v2.3（同日，第三轮评审）：撤回全平台大小写不敏感（`git branch -d` 会放行 `-D`），规则包按平台拼装；订正“批量遍历跳过 `ask`”——现有实现只过滤 `deny`，实测 `grep` 会返回 `ask` 文件的内容，列为 PR 3 的代码改动；scope 带上 `flags`；审批“占有”后超时 / 取消不再竞争；补 `Workspace.local(baseline=, shell_default=)` 的 API 变更。
>
> 行号指向 `main`（`1cbe294`，v0.9.132）。

## 结论

lovia 的三段式判定（`allow` / `ask` / `deny`）和失败即拒绝的审批通道都是对的，问题在于**判定结果太瘦、默认档太粗、授权在错的地方**：

- 判定只传一个 `bool`，卡片说不出“为什么问、后果是什么”，模型也收不到“为什么被拒”。
- `coding` 档的 shell 默认全问（`shell_default="ask"`，无内置白名单），`ls`、`git status`、`pytest` 都要确认——正是 kits 实测里 12/16 次“本不该问”的那种确认。
- 任何档位都没有不可逾越的底线：`trusted` 下 `git push --force`、写 `.git/config`、读 `~/.ssh` 都直接放行。
- “总是允许”做在浏览器端、按**工具名**记（勾一次 `shell` 等于放开所有命令）、只活在当前标签页，界面却写着“本对话”。这是 kits 实测出的 `always` 漏洞里的两个（过宽、作用域文案不实）；第三个（盖过 deny）lovia 没有，session 对 `deny` 的强制不受前端影响。

改法是**把同一套词汇贯穿到底**，不加新的扩展轴：

1. 判定结果从 `Decision` 字面量升级为 `Verdict(action, reason, scope)`——多两个字段，一个给人和模型看理由，一个说明“总是允许”会放开多大范围。
2. `Tool.needs_approval` 改名为 `Tool.permission`，接受 `Decision | Verdict | predicate`。工具作者可以直接说 `"deny"` 并给理由。
3. 工作区策略加一条 `baseline`（所有预设都生效的硬规则：黑名单命令、密钥路径、受保护配置的写入），**与普通判定取最严格者**，只能加严不能放松。`coding` 预设内置一组不可撤回动作的询问规则（判错的代价只是多问一句）；只读命令和开发工具的放行包是**可选项**（v3 第八轮改为可选：框架不替任何程序担保，少弹窗靠部署方启用规则包或用户按对话授权），shell 默认档仍是 `ask`。
4. 审批通道的决定也带理由：`ev.reject(reason=...)`，模型收到的是“用户拒绝了：...”而不是干巴巴的 "not approved"。
5. 授权（grants）**不进 core**。core 只负责在 `Verdict.scope` 里说清范围；web 在服务端按聊天持久化、按 scope 精确匹配、只对 `ask` 生效、可撤销。非 web 的消费者用 `approval_handler` 五行实现同样的事。

不做审查模型，不做沙箱（已有 `ShellExecutor` 缝），不做 agent 级总闸（#261 仍然缺一个具体消费者），不做完整的 shell 解析器。

## 现状

| 层 | 位置 | 现在怎么做 |
|---|---|---|
| 工具 | `Tool.needs_approval: bool \| (args, ctx) -> bool`（`tools/base.py:106`） | 工具作者决定要不要问；只能问，不能拒 |
| 运行时 | `ToolCallProcessor.preflight()`（`runtime/tool_calls.py:227-275`） | 谓词为真 → 发 `ApprovalRequired(call)`，等流式消费者 / `Agent.approval_handler(call, ctx)` / 默认拒绝；拒绝文案固定为 `Tool X was not approved.` |
| 通道 | `ApprovalChannel`（`approvals.py`） | `(scope, call_id) → Future[bool]` |
| 工作区 | `WorkspacePolicy`（`workspace/policy.py`）+ `command_guard.py` | 路径 ACL（有序，首个命中）+ 命令前缀规则（首个命中）+ 词法路径声明，最严格者胜；`deny` 在 session 里强制，`ask` 由内置工具的谓词转成审批 |
| web | `ApprovalRegistry`、`POST /api/chat/approve {approve\|deny}`、`chat.js:840-920` | 卡片只显示工具名和参数；“本对话中总是允许 {name}” 是浏览器内存里的 `Map<session, Set<toolName>>`；断线不拒绝（审批在 supervisor 任务里等，重连后重发卡片，`tests/web/test_supervisor.py:317`），取消和超时才拒绝 |

这套结构的好处要保住：**preflight 串行、失败即拒绝、`deny` 在 session 强制而不信任工具层、事件是纯数据、通道独立、断线不丢审批**。本文不动这些。

### 具体缺口

1. **卡片无理由。** `ApprovalRequired` 只有 `call`。kits 的卡片原则是“先说后果，再给命令；说明为什么问”，没有数据做不到。
2. **拒绝无反馈。** 用户拒绝后模型只看到 "not approved"，常会换个写法再试一次同样的事。Cursor 的经验：把受限原因写进工具结果后恢复明显更好。
3. **“总是允许”粒度错、位置错。** 按工具名记等于 `shell *`；存在标签页里，换个标签页就没了。
4. **默认档是弹窗风暴。** `coding` 没有任何内置 allow 规则。
5. **没有底线。** 硬拒绝只靠部署方自己写 `CommandRule("rm -rf", "deny")`；`trusted` 什么都放。
6. **`approval_handler` 看不到判定。** 签名是 `(call, ctx)`，想做“按范围自动放行”或“LLM 审查”都缺上下文。
7. **前缀规则有两个已验证的空子。** `FOO=1 git status` 和 `env git status` 都匹配不到 `git status`（落到默认档）；`git push origin main --force` 匹配不到 `git push --force`（旗标不在开头）。前者让绿名单漏放行、后者让黑名单漏拦。

## 设计

### 一套词汇：`Decision` 不变，加 `Verdict`

```python
# 从 lovia/workspace/policy.py 上提到 lovia/approvals.py：core 不能 import workspace，
# 而 tools 和 preflight 都要用；approvals.py 本来就是“审批词汇 + 通道”的家。
# workspace.policy 继续 re-export Decision。
Decision = Literal["allow", "ask", "deny"]

@dataclass(frozen=True)
class Verdict:
    """策略判定：这个调用能不能做、为什么、“总是允许”会放开多大。"""
    action: Decision
    reason: str = ""          # ask 时给人看，deny 时给模型看
    scope: str | None = None  # 可记忆的授权范围；None = 不提供“总是允许”

def as_verdict(value: object) -> Verdict:
    """归一化：Decision 字面量或 Verdict；其余（包括 bool）抛 TypeError。"""
```

`as_verdict` 是唯一的归一化点，preflight 和工作区的合并都用它。**策略端不接受 bool**：`needs_approval` 时代 `True` 是“要问”，但 `permission=lambda a, c: ctx.deps.can_delete` 这种写法里 `True` 直觉上是“可以做”，两种读法正好相反，接受它只会制造反向 bug。非法返回值和异常走现有的失败即拒绝路径：`ToolCallFailed` 事件 + `deny`（理由 `permission check failed: ...`），开发者在事件流和日志里看得到。审批答复（`approval_handler`、通道）里的 bool 保留，那边“True = 批准”没有歧义。

规则对象加可选字段：`CommandRule(pattern, action, reason="", grant=True)`、`PathRule(..., reason="", grant=True)`。`reason` 不写则生成默认理由（`matches rule 'git push'`、`not in the allow list`、`reads outside the workspace root: ~/.ssh`）；`grant=False` 表示命中这条 `ask` 规则时不提供“总是允许”（见“范围”）。

`WorkspacePolicy.decide_path` / `decide_command` 和 session 的同名方法改为返回 `Verdict`；`merge_decisions` 改为 `merge_verdicts`，最严格者胜。`command_decider` 的返回类型放宽为 `Decision | Verdict | None`。

### 工具层：`Tool.permission`

```python
Permission = Decision | Verdict | Callable[[dict[str, Any], RunContext[Any]], Decision | Verdict]

@dataclass
class Tool:
    permission: Permission = "allow"
```

```python
@tool(permission="ask")  # 以前 needs_approval=True
async def send_email(to: str, body: str) -> str: ...


@tool(permission=Verdict("ask", "Sends mail outside the company."))  # 静态理由
async def send_external(to: str, body: str) -> str: ...


def refund_policy(args, ctx):
    cents = args["amount_cents"]
    if cents > 1_000_000:
        return Verdict("deny", "Refunds above 10,000 USD go through finance, not the agent.")
    if cents > 5_000:
        return Verdict("ask", f"Refund of {cents / 100:.0f} USD exceeds the 50 USD limit.")
    return "allow"

@tool(permission=refund_policy)
async def refund(order_id: str, amount_cents: int) -> str: ...
```

改名的理由：谓词现在能返回 `deny`，`needs_approval` 这个名字就不诚实了；`permission="ask"` 比 `needs_approval=True` 读得更顺，和工作区、MCP 用的是同一套词。`MCP(needs_approval=)`、`schedule_run`、内置工作区工具同步改名；`path_needs_approval(*ops)` → `path_permission(*ops)`，并且 `deny` 时直接返回 deny 判定——模型拿到理由，什么都没执行。session 的强制不变，仍是自定义工具绕不过的底线。

不留 `needs_approval` 兼容名。[stability.md](../en/stability.md) 的政策是 pre-1.0 破坏性变更随 patch 发布，这也是项目既定做法；AGENTS.md 里“保留 ≥ 1 minor release 的 shim”那句写的是 1.0 之后的规则，顺手把措辞改一致（“From 1.0 on”）。

### 运行时：preflight 里的判定与解决

```text
preflight(call)
  verdict = as_verdict(tool.permission(args, ctx))          # 异常 / 非法值 → deny
  ├─ deny  → _rejected(call, f"Denied by policy: {reason}")
  ├─ allow → ready
  └─ ask   → fut = approvals.register(call.id)
             yield ApprovalRequired(call, reason, scope)          # 流式消费者可当场决定
             if not fut.done(): approval_handler(request, ctx)   # 程序化决定
             if not fut.done(): reject(reason="no approver")     # 没人决定 → 拒绝
             approved → ready
             denied   → _rejected(call, denial_text(approval))
```

拒绝文案只有两种，具体来源由 `reason` 说，运行时不猜“是不是用户拒的”（通道看不出来，猜错就是误导模型）：

| 来源 | 工具结果 |
|---|---|
| 策略 `deny` | `Tool X was denied by policy: {reason}` |
| 任何 `ask` 之后的拒绝 | `Tool X was not approved: {reason}`；无理由则 `Tool X was not approved.` |

理由由拒绝方填：用户在卡片上写的话原样进去；web 的超时写 `approval timed out`、取消写 `the run was cancelled`、运行结束写 `the run ended`；没人回答写 `no approver available`；handler 抛异常写 `the approval handler failed`，返回非法值写 `the approval handler returned an invalid decision`。多个 `ask` 条件合并时理由用 `; ` 连起来（`no workspace rule allows this command; reads outside the workspace root require approval (/etc)`）。

签名变化：

- `ApprovalRequired` 加 `reason: str`、`scope: str | None`。
- `Agent.approval_handler` 的签名从 `(call, ctx)` 改为 `(request: ApprovalRequired, ctx)`，与 hooks / guardrails 的 `handler(event, ctx)` 一致；返回 `ApprovalDecision = bool | Decision | Approval`（见下）。`"ask"` 表示不决定，交给默认拒绝。handler **不返回 `Verdict`**：它不是在判定策略，而是在回答这一次请求。

判定只在每次模型请求时做一次，不随重试重做——这是 `permission` 留在 preflight、不做成 `ToolPolicy` 的原因（#261 里分析过的三个坑：无调用身份、在审批之后、每次重试都跑）。

### 通道：决定也带理由

```text
@dataclass(frozen=True)
class Approval:
    """对一次审批请求的决定。和 Verdict 分开：Verdict 是“该不该问”，Approval 是“问了之后的答复”。"""
    approved: bool
    reason: str = ""     # 拒绝理由，原样进工具结果

class ApprovalChannel:
    # Future[bool] → Future[Approval]；消费者读 .approved，不做真值判断
    def approve(self, call_id, *, scope=None) -> None
    def reject(self, call_id, *, scope=None, reason: str = "") -> None
    def resolve(self, call_id, decision: bool | Approval, *, scope=None) -> bool

class ApprovalRequired:
    def approve(self) -> None
    def reject(self, reason: str = "") -> None
```

`Approval` 是 #40（批准时改参数）将来挂 `arguments` 的位置，本次不加。

### 工作区

#### `baseline`：所有预设都生效，只加严不放松

```python
baseline: tuple[PathRule | CommandRule, ...] = DEFAULT_BASELINE
```

判定顺序：

```text
普通判定 = denied_paths → path_rules / command_rules（用户规则在前，预设规则在后；首个命中）→ 默认值
最终判定 = merge_verdicts(baseline 判定, 普通判定)        # 最严格者胜
```

baseline 不参与“首个命中”，它是独立算出来再合并的，所以：用户规则里的 `deny` 不会被 baseline 的 `ask` 冲掉；`readonly` 的 `write="deny"` 遇到 baseline 的 `.git 写 → ask` 仍是 `deny`；`command_decider` 再怎么返回 `allow` 也绕不过 baseline；`allow_shell=False` 仍然是最外层的硬约束。授权只对 `ask` 生效，baseline 的 `deny` 不可能被“总是允许”盖掉。

正因为 baseline 只能加严，**放进去的必须是部署方不该在不知情时翻掉的东西**，而且要能按组摘掉而不是全有全无：

```python
DANGEROUS_COMMANDS  # deny：git push --force、rm -rf /、format、diskpart …
CREDENTIAL_PATHS    # deny：~/.ssh、~/.aws、*.pem …
PROTECTED_CONFIG    # ask（写）：.git、.github/workflows、.lovia …
DEFAULT_BASELINE = DANGEROUS_COMMANDS + CREDENTIAL_PATHS + PROTECTED_CONFIG

Workspace.local(root, baseline=DANGEROUS_COMMANDS + PROTECTED_CONFIG)   # 这个 agent 要管 ~/.ssh/config
```

注意最后一行摘掉的是**整组** `CREDENTIAL_PATHS`（`~/.aws`、`*.pem` 一起放开），不是只放开 `~/.ssh/config`；只想开一个路径就自己拼：`baseline=DANGEROUS_COMMANDS + PROTECTED_CONFIG + tuple(r for r in CREDENTIAL_PATHS if r.pattern != "~/.ssh")`，再用 `readable=("~/.ssh/config",)` 给到想要的那一个。`Workspace.local` 现在没有 `baseline=` 和 `shell_default=` 参数，两个都是本方案新增的（见“破坏性变更”）。

`.env*` **也在 baseline 里**（v3 改回；v2 曾放在预设层，两轮评审各发现一次它降低了 readonly 的限制——这正是“保护规则放在可覆盖层”的结构问题）。规则是 `PathRule("**/.env*", "ask", ops={"read"})`：只管读、只在工作区内（`**/` 是工作区相对的，裸 `.env*` 会匹配到根目录外），而且走 baseline 的合并，所以不可能放松任何模式默认值。用户让 agent 配环境变量、排查启动失败时读 `.env` 再正常不过，放开方式：

| 谁 | 怎么做 |
|---|---|
| 聊天里的用户 | 卡片上点“允许”；勾“本对话中总是允许 `**/.env*`”则这个聊天不再问（scope `read:**/.env*`） |
| 部署方 | `Workspace.local(root, baseline=DANGEROUS_COMMANDS + CREDENTIAL_PATHS + PROTECTED_CONFIG)` —— 不带 `ENV_FILES` 重新拼底线 |
| 部署方，想反过来彻底禁止 | `denied_paths=(".env*",)` |

**批量遍历要补一条过滤，这是代码改动，不是现状。** architecture.md 写的“遍历中非 `allow` 即跳过”只对符号链接目标成立；`_read_denied()`（`local.py:394`）只过滤 `deny`。实测：`PathRule(".env*", "ask")` 下 `read_file(".env")` 判 `ask`，但 `grep("TOKEN", include_hidden=True)` 直接返回了 `.env` 的内容。默认不扫隐藏文件只是默认值，不是权限。PR 3 要改成：`list` / `glob` / `grep` 的遍历对每个条目检查**自身和解析后目标**的读取判定，非 `allow` 一律跳过、不弹卡片（遍历中没法审批）；用户点名的 `read_file(".env")` 照常走这一次调用的审批。回归用例覆盖 `include_hidden=True`、嵌套目录、符号链接三种。

顺带一个边界要写清：web 的授权不改底层 `WorkspacePolicy`，“记住读取 `.env*`”只让之后**直接读取**它的调用自动批准，批量 `grep` 仍然跳过它——想让 grep 也看到，部署方要把规则改成 `allow`。

#### 命令规则：前缀不变，加一个显式的 `flags=`

`CommandRule.pattern` 仍然是**词边界前缀**，一个词不多。v2 曾想把“第一个旗标之后的词都可以在任意位置”做成通用语义，评审推出了反例：`CommandRule("python -m pytest", "allow")` 会命中 `python -m pip install pytest`，因为 `pytest` 是 `-m` 的参数、不能和 `-m` 拆开。让 deny 多命中几种写法的同时让 allow 也扩大，不值。

已验证的漏洞只有一类：旗标不在开头（`git push origin main --force`）。为它加一个显式字段，别的不动：

```python
@dataclass(frozen=True)
class CommandRule:
    pattern: str                       # 词边界前缀，同今天
    action: Decision
    reason: str = ""
    grant: bool = True
    flags: tuple[str, ...] = ()        # 每个都必须出现在命令的任意位置；单字母短旗标按字母匹配（-rf 满足 -f）
```

```text
CommandRule("git push", "deny", flags=("--force",))
  git push --force origin main      → 命中
  git push origin main --force      → 命中
  git push --force-with-lease       → 不命中（整词相等）
CommandRule("git push", "deny", flags=("-f",))
  git push -f origin                → 命中
  git push -fu origin               → 命中（字母 f 在短旗标组里）
CommandRule("rm -rf /", "deny")     → 仍是前缀：rm -fr / 和 rm -r -f / 各写一条，见附录
```

`flags=` 只认旗标词（`-x`、`--xx`、`/x`），不认位置参数，所以写不出 `python -m pytest` 那种歧义；一个裸的 `--` 之后的词不再算旗标。它的用途就这么窄：服务内置的几条 `deny` 和部署方写得出的少数 `ask`，不是通用参数解析器——`-m msg` 这种带参数的短选项、`--name=value` 都不在它的能力范围内，也不需要。

匹配**区分大小写**，和今天一样。v2.1 曾改成全平台不区分，推理只看了命令名，漏了旗标：`CommandRule("git branch -d", "allow")` 会放行 `git branch -D feature`，`-d` 要求已合并、`-D` 是强删，这是 git 自己的参数语义，和宿主文件系统怎么比较文件名无关；pattern 里还可能有模块名、路径、分支名。“Linux 上 `RM` 不存在”也不是可靠前提。cmd.exe 的命令名确实不区分大小写，但模型几乎总写小写，`DIR /b` 落灰区问一次没有坏处；如果以后 Windows 用户反馈多，只对 CMD 包的**命令名**做不区分，旗标和参数照旧。

**剥赋值和包装：剥了就最多到 `ask`。** 段首的 `VAR=value` 和一小组固定形式的包装（`env` 后直接跟命令、`nice`、`nohup`、`time`、`timeout <n>`、Windows 的 `call`）剥掉后再匹配规则，但只要剥过，结果 `merge(verdict, "ask")`：`deny` 仍是 `deny`，`allow` 降为 `ask`。理由是剥掉的东西确实会改变执行（`PATH=`、`LD_PRELOAD=`、`PYTHONPATH=` 都能把 `git status` 变成别的程序），不能用它去命中 allow；但 `env git push --force` 该拒还得拒。`env -i`、`env -u X` 这类带参数的形式不剥（整段落灰区）；`sudo`、`xargs`、`sh -c`、`cmd /c`、`powershell -Command`、`wsl`、`start` 不是包装，是换了解释器或语义，各走自己的规则。

**Windows 上 lovia 默认的本地执行后端用的是 `cmd.exe`**（`create_subprocess_shell` 走 `COMSPEC`，`local.py:1003`），不是 PowerShell；自定义 `ShellExecutor` 不在此列。所以 Windows 侧要覆盖的是 cmd 内建和常见外部命令：`dir`、`type`、`findstr`、`where`、`del`、`rd` / `rmdir`、`copy`、`move`、`format`、`diskpart`、`shutdown`，旗标是 `/s /q /f` 这种 `/` 开关。PowerShell 只会以 `powershell -Command ...` / `pwsh -c ...` 的形式出现，它是解释器，走灰区、不可记忆，和 `python -c` 同等对待；不做 PowerShell 别名归一。分隔符 `&`、`&&`、`||`、`|` 和重定向 `>` 现有的 `_COMMAND_SPLIT` 已经覆盖。

一并修一个已知误报：`command_guard.py` 在 Windows 上把 `dir /b` 的 `/b` 当成根路径、算作工作区外读取，`coding` 下每条带开关的 cmd 命令都会多问一次（#344 的描述里记了，未立 issue）。规则：Windows 上 `/` + 一到两个字母（可带 `:x` 后缀，如 `/a:d`）的词是开关，不是路径声明。

这不是 shell 解析器。`$(...)`、`%VAR%`、`eval`、解释器一行程序、`xargs` 的参数、`git -C dir push` 这种全局选项插在中间的写法仍然看不透（后者落灰区，会问），`command_guard.py` 的 advisory 声明照旧。承诺收窄成一句真话：**baseline 拦的是这些命令的这些写法，不是“所有强制推送”**；它针对的是模型主动写出的危险命令，不是有意绕过的命令，也不是它所运行的代码内部做的事。

#### 规则包与预设

规则按用途打包成常量，预设只是把包拼起来的语法糖；想换组合直接用 `WorkspacePolicy(...)` 拼：

```python
# lovia/workspace/rules.py — 公共包 + 每个平台一个小包，预设按 os.name 拼
READ_ONLY         # 跨平台：git status / diff / log / show / blame / rev-parse / ls-files
READ_ONLY_POSIX   # ls cat head tail wc grep rg pwd echo which stat du df date sleep test tree diff sort uniq cut
READ_ONLY_CMD     # dir type findstr where echo cd tree fc more        （fc 在 cmd 里是文件比较，在 bash 里是重跑历史命令）
DEV_TOOLS         # 跨平台：pytest, python -m pytest, ruff check, ruff format, mypy, npm test, npx tsc,
                  #         cargo test / check / build, go test / vet / build
IRREVERSIBLE      # ask：git push、git reset --hard、git clean、git branch -D、
                  #     gh pr create、gh release、npm publish、twine upload、cargo publish（grant=True）
                  #     sudo、ssh、scp、rsync、docker、kubectl（grant=False）
IRREVERSIBLE_POSIX / IRREVERSIBLE_CMD   # rm -r ｜ del /s、rd /s、runas、net user、reg（grant=False）
DANGEROUS_COMMANDS, CREDENTIAL_PATHS, PROTECTED_CONFIG, DEFAULT_BASELINE   # 见 baseline 一节；黑名单同样分 _POSIX / _CMD
```

规则包**按平台拼装**，预设里一个 `if os.name == "nt"` 选小包。v2.1 曾想两边条目合在一个包里省掉分支，评审举了 `fc` 的反例：同名不等于同一种能力，一个平台分支比一组错误的等价关系简单。`command_guard.py` 的分词本来就按平台（#344：Windows 不把反斜杠当转义）。纯规则匹配的测试不依赖平台，可以在任何机器上参数化地跑两套包；只有分词和真实执行要靠 CI 的 Windows 任务。

v1 的绿名单删掉了这些：`find`（`-delete` / `-exec`）、`sed` / `awk`（`-i` 写文件、`system()`）、`env`（已是包装，剥掉即可）、`make`、`npm run`（任意项目脚本）、`git branch`（`-D`）。它们回到灰区，每次问人；部署方想免问或想可记忆，各写一条规则。

`DEV_TOOLS` 要如实说明：放行 `pytest` 就是**信任项目代码在宿主上执行**。这本来就是 coding agent 的前提（能写文件、能跑一条命令，就能执行任意代码），baseline 保护的是模型*直接*动手的那一步，不是它跑起来的代码内部做的事。不想要这层信任的部署方：`WorkspacePolicy(command_rules=user + IRREVERSIBLE + READ_ONLY, ...)`。

| | `readonly` | `coding`（默认） | `trusted` |
|---|---|---|---|
| baseline | ✓ | ✓ | ✓（`.git` 等受保护路径的写入每次都问、不可记忆；“不打断”说的是命令） |
| `IRREVERSIBLE`（ask） | 无 shell | ✓ | ✗ |
| `READ_ONLY` + `DEV_TOOLS`（allow） | 无 shell | ✓ | ✓ |
| 灰区（其余） | 无 shell | **ask** | allow |
| 工作区外读 / 写 | deny / deny | ask / deny | allow / ask |

`coding` 的灰区仍然问人，这是和 kits 落地的唯一实质差别。kits 是产品，“拦住最危险的就够了”；lovia 是框架，默认档应该是不骚扰也不越权的那一档，想要 kits 的姿态一个旋钮就到：`Workspace.local(root, mode="coding", shell_default="allow")`。kits 的 16 次确认是 6 个会话的局部观察，足以说明疲劳存在，不足以证明更宽的默认放行适合所有消费者。

#### 范围（scope）由判定方生成

scope 既是显示文本也是匹配键，精确字符串匹配，不做前缀或 glob。生成规则：

| 判定来源 | scope | 例 |
|---|---|---|
| 显式 `ask` 规则命中，`grant=True`（默认） | 规则的 pattern，加上 `flags`（有的话） | `shell:git push`、`shell:git push --tags`、`write:.github/workflows` |
| 显式 `ask` 规则命中，`grant=False` | 无 | `sudo`、`ssh`、`docker`：放开首词等于全放开，内置红名单对这些关掉 |
| 灰区命令（`shell_default`，没有规则命中） | 命令词 `shell:<cmd>`（v3 改回：框架不再自带放行包后，这是普通用户少弹窗的唯一途径，范围按原样显示、按对话记录、可撤销；参数不确定时无） | `make deploy` → `shell:make`；`cat *`、`FOO=1 ls` → 无 |
| 工作区外读 / 写（默认档） | op + 目标所在目录（`~` 折叠） | `read:~/notes` |
| 需要批准的条件不止一个 | 无 | `git push && python deploy.py` |
| `deny` | 无 | |

三条要点。第一，范围**只来自规则作者**：规则的 pattern 就是授权的粒度，不自动截取。v2 曾让灰区命令取首词并靠一张 `NO_GRANT` 名单排除解释器，评审指出 `.venv/bin/python`、`/usr/bin/python3` 这类完整路径写法一条都排不掉，名单永远追不上执行器的数量——所以灰区干脆不给 scope。部署方想让 `make` 可记忆，写一条 `CommandRule("make", "ask")` 即可；`DEV_TOOLS` 已覆盖主要开发动作，这不会回到弹窗风暴。工作区外读写的目录范围保留：它是“目录”这种边界清楚的东西，不是开放式执行器。第二，`git push origin feature` 和 `git push other main` 都落在 `shell:git push` 下，卡片上写明“总是允许 `git push`（含其他远端和分支）”，用户同意的就是这个范围；想按远端、分支授权，写更长的规则。scope 必须带上 `flags`：`CommandRule("git push", "ask", flags=("--tags",))` 和 `flags=("--delete",)` 是两条规则、两种动作，只取 pattern 会让记住“推 tags”顺带放开“删远端分支”，所以键是 pattern 和 flags 的规范化拼接，界面也显示完整条件。第三，`merge_verdicts` 只在**恰好一个** `ask` 条件时保留它的 scope；两个 `ask` 合在一起 scope 为 `None`，一条已授权的范围不可能顺带放过另一条没授权的条件。

显示和键不拆成两个字段：键本身可读（`shell:git push`），界面做格式化（“总是允许 `git push`（本对话）”）。

#### 模型侧

`Workspace.instructions()` 加一句：命令和路径可能需要用户批准或被策略拒绝；拒绝会给出理由——遵守它，选一个仍符合策略的做法，不要把同一个被禁止的操作换个写法再试。

### 授权（grants）：core 只给范围，不存状态

“本会话总是允许”是跨 run 的可变状态，core 里只有 `RunLoop` 持有可变状态，而且 run 不是“会话”。所以 core 不做授权存储，只保证两件事：`Verdict.scope` 在事件上、`approval_handler` 能看到它。非 web 的消费者这样写：

```python
granted: set[str] = set()

def remember_grants(req: events.ApprovalRequired, ctx: RunContext) -> Decision:
    return "allow" if req.scope in granted else "ask"

agent = Agent(..., approval_handler=remember_grants)
# 用户批准并勾“总是”时：granted.add(req.scope)
```

### Web

**服务端授权。** 存储是一张小表 `chat_grants(session_id, scope, context, created_at)`，主键 `(session_id, scope, context)`：加、删都是单行操作，两个标签页同时批准、撤销不会互相覆盖。

`context` 是授权成立时的工作区身份：agent 名和解析后的根目录，用无歧义的编码拼起来（JSON 数组或长度前缀，不是裸 `@` 拼接——agent 名里可以有 `@`），取值来自**这次审批所属的活动 agent 和已绑定的工作区**，不是请求开始时的配置。一个聊天并不终生绑一个工作区——`WorkspaceSessions.bind()`（`web/workspaces.py:76`）在聊天切到 root 或 policy 不同的 agent 时会关旧开新；绑定表又是进程内存，重启后换了配置也无从比对。所以匹配时同时要求 `scope` 和 `context` 相等：换了 agent 或根目录，旧授权**暂时不适用**但仍在表里，切回来继续有效；用户在列表里删掉才是**撤销**。两种状态界面分开写。`context` 不含策略指纹：规则收紧（`ask` → `deny`）时授权本来就不生效，同名 agent、同一根目录下再把规则放回 `ask`，旧授权会重新生效——这是接受的残余，列表里看得见、删得掉。

生命周期契约：

| 环节 | 规则 |
|---|---|
| 来源 | scope 和 context 只从**服务端持有的待审批请求**上取；客户端只发 `remember: true`，不发范围字符串 |
| 顺序 | 先**占有**待审批请求（`ApprovalRegistry` 原子地把它标成“处理中”，第二个请求得到 404），再写授权，**最后**才 `resolve` 唤醒 runner——否则下一条同范围的调用可能在落库前又弹卡片。写授权失败：本次调用照用户的意思批准，响应里 `remembered: false`，前端提示“已允许，但未能记住” |
| 占有 vs 超时 / 取消 | 占有成功就是**接受了用户决定的时点**：此后 `approval_timeout` 和重复请求都不再碰这个 future（超时等待者看到“已占有”就继续等占有者）；占有者在 `finally` 里必定 `resolve`，成功路径批准、自身异常则拒绝，所以请求不会永久停在“处理中”，HTTP 断线也一样（写库和 resolve 不在请求任务里 await 取消）。用户在此期间取消运行：run 照常停（preflight 在审批通过后再查一次 `cancel_token`——PR 2 补的一行，之前只在审批前查，批准后的调用会先跑完再停），**已落库的授权保留**——那是用户明确做出的决定，不该由时序碰巧决定它存不存在；不想要就在列表里删。这个窗口要有一个“落库期间触发超时 / 取消”的测试 |
| 有效性 | 已超时、已取消、已处理、不存在的请求：404，不写授权 |
| 继承 | 新聊天、子代理聊天（`parent_id` 非空）不继承；子代理本来就是无客户端运行，审批走 `approval_handler` 或拒绝 |
| 撤销 | 只影响之后的调用；已在执行的动作撤不回，界面不暗示能撤回 |
| 匹配 | `RunSupervisor` 收到 `ApprovalRequired` 时查表：`scope` 非空、`(scope, context)` 命中 → `ev.approve()`，`approval_required` 帧带 `granted: true`（前端复用现有的 `approval.auto` 一行提示，不弹卡片） |

`chat.js` 里的 `_autoApprove` 删掉。

**卡片。** 按 kits 的原则重排：

```text
┌ 需要你的确认 ───────────────────────────────┐
│ 推送到远端仓库，不可撤回。                      │   ← verdict.reason
│ shell  git push origin main                   │   ← 工具 + 参数（原样；摘要可能错，命令始终显示）
│ ☐ 本对话中总是允许  git push                   │   ← 仅 scope 非空时显示，显示精确范围
│ 拒绝原因（可选，模型会看到）[________________]  │
│                              [拒绝]  [允许]   │
└──────────────────────────────────────────────┘
```

Enter 仍然是允许（焦点在允许键上，`focusApprovalCard` 不变）。问得少之后每一次都值得看，不再给红级加快捷键摩擦。

**接口（都是增量）。**

- SSE `approval_required` 的 `ToolCallData` 加 `reason`、`scope`、`granted`。
- `POST /api/chat/approve` 的 `ApprovalRequest` 加 `remember: bool = False`、`reason: str = ""`；响应加 `remembered: bool`。
- 授权的列出 / 撤销：`GET /api/sessions/{id}/grants`（每条带 `scope`、`context`、是否适用于当前工作区）、`DELETE /api/sessions/{id}/grants?scope=...&context=...`；入口放在聊天的设置面板里（“已允许的规则”列表，每条可撤销，不适用的灰显）。

**不变**：断线不拒绝（重连后重发卡片）、取消和 `approval_timeout` 拒绝、`await_approvals=False`、子代理默认拒绝。

### 事后可见

`deny` 判定本来就以 `is_error` 的工具结果落在时间线上，带上理由后活动面板的工具行自然显示“被策略拒绝：...”。不做 kits 列的“总结卡”“被拦下列表”——kits 自己最后也没做，理由一样：问得少之后价值不大。

## 不做什么

| 项 | lovia | 为什么 |
|---|---|---|
| 审查模型（灰区交 LLM） | 不做，留缝 | `approval_handler(request, ctx)` 可 async、能看到判定和 run 上下文，一个 LLM 审查器就是一段 recipe（写进 `docs/tools.md`）。kits 自己也判定“多一次模型调用换来拦下本来就很少的危险命令”不值 |
| 沙箱 | 不做 | `ShellExecutor` 缝已经有（`protocol.py`），策略门之后跑 |
| 完整 shell 解析（tree-sitter） | 不做 | 有依赖；`$(...)` 和解释器一样看不透 |
| 通用的“必含词任意位置”规则语言 | 不做（v2 提过，撤回） | `python -m pytest` 对 `python -m pip install pytest` 的反例说明位置参数不能脱离旗标；只保留窄的 `flags=` |
| 灰区命令自动生成授权范围 | 不做（v2 提过，撤回） | 靴子名单追不上 `.venv/bin/python` 这类完整路径写法；范围只能来自规则作者 |
| agent 级总闸（任意工具的 allow/ask/deny） | 不做，#261 保留 | 本次的消费者都在工具层或工作区层：自有工具用 `permission=`，MCP 用 `MCP(permission=)`，shell / 文件用 `WorkspacePolicy` |
| 按完整调用（工具 + 规范化参数 + cwd）记授权 | 不做 | 对 shell 几乎永远不命中第二次，等于没有“总是”；按规则 pattern 记、显示出来，是 Claude Code / OpenCode / kits 共同的做法。粒度交给规则作者 |
| scope 的键和说明拆成两个字段 | 不做 | 键可读；格式化是前端一行代码 |
| 合并多张卡片 | 不需要 | preflight 串行，同一时刻只有一张 |
| 批准时改参数（#40） | 不做 | `Approval` 是将来挂 `arguments` 的位置 |
| `decidedBy` / 风险级别写日志 | 不做 | 需要审计的消费者用 hooks |
| 规则管理页 | 最小版 | 只做“已允许的规则”列表 + 撤销 |
| `needs_approval` 兼容名 | 不做 | 项目既定：1.0 前不留 shim |

工作区策略保持**有序、首个命中**，不改成按动作优先级（deny > ask > allow）的无序规则——规则是部署方写的配置，顺序即优先级最直观；底线靠 baseline 的独立合并，不靠优先级。

## 破坏性变更（pre-1.0，直接改，PR 里列出）

- `Tool.needs_approval` → `Tool.permission`；`@tool(needs_approval=)`、`MCP(needs_approval=)`、`DeferredMCP(needs_approval=)` 同名改。
- `ApprovalHandler` 签名 `(call, ctx)` → `(request, ctx)`；`ApprovalDecision` = `bool | Decision | Approval`。
- `WorkspacePolicy.decide_*`、`WorkspaceSession.decide_*` 返回 `Verdict`；`merge_decisions` → `merge_verdicts`；`CommandDecider` 返回类型放宽。
- `CommandRule` 加 `reason` / `grant` / `flags` 字段，`PathRule` 加 `reason` / `grant`；pattern 的前缀语义和大小写敏感不变。带前导赋值或包装的段剥掉后最多到 `ask`（以前是落默认档）。
- `WorkspacePolicy` 加 `baseline`；`Workspace.local()` 加 `baseline=` 和 `shell_default=` 两个参数（现在都没有）。
- `list` / `glob` / `grep` 的遍历跳过非 `allow` 的条目（以前只跳过 `deny`）。
- `coding` 预设的实际行为变化：`READ_ONLY` / `DEV_TOOLS` 不再问；所有预设多了 baseline。
- `ApprovalChannel` 的 future 类型（`lovia.runtime` 之外的消费者很少直接碰）。

## 分期

三个 PR 分别回答三个问题：**说清楚**、**记住批准**、**默认放行更多**。拆开评审，哪一步改善了什么、哪一步扩大了什么，一眼可见。

| PR | 内容 | 规模 |
|---|---|---|
| 1 core：说清楚 | `Verdict` / `as_verdict` / `Approval`；`Tool.permission` 改名；`ApprovalRequired.reason/scope`；`reject(reason)`；`approval_handler(request, ctx)`；分来源的拒绝文案；`command_decider` 放宽；工作区 `decide_*` 返回 `Verdict` 并生成理由和 scope（**不改默认规则**）；`instructions()` 一句；docs（tools / mcp / workspace / README ×2）、`examples/12_approval.py`；AGENTS.md 措辞 | 中 |
| 2 web：记住批准 | `chat_grants` 表 + supervisor 查询 + 两个接口；卡片改版（理由、scope 勾选、拒绝原因）；删 `_autoApprove`；SSE / schema 增量；http-api.md、web-ui.md | 中 |
| 3 workspace：默认值 | `baseline` 字段 + 最严格合并；命令规则的旗标语义与剥包装；`rules.py` 四个规则包；预设重排；`test_policy.py` / `test_command_guard.py` / `test_workspace_tools.py` | 中大 |
| 后续 | MCP 是否按 `annotations.destructiveHint` 默认 ask；LLM 审查 recipe | 小，各一 PR |

PR 3 的 Windows 条目（大小写、`/x` 开关、cmd.exe 规则、`dir /b` 误报）只能在 CI 的 `test-windows-env` 任务里验证，用例放 `tests/workspace/test_policy_windows.py`，macOS 跑不了。

## 验收：先验不变量，再验名单

每条名单“能命中一次”的用例当然要有，但先写这些：

1. baseline 从不放松既有限制：对任意 `(policy, baseline)`，`merge(baseline, 普通)` 的严格度 ≥ 普通判定。
2. 授权从不覆盖 `deny`：`deny` 判定的 scope 恒为 `None`；supervisor 对 `deny` 不查表。
3. 多个审批条件不能被部分授权覆盖：两个 `ask` 合并后 scope 为 `None`。
4. 过期 / 已处理的审批不能写入授权：`resolve` 返回 `False` 时表无变化。
5. 规则收紧后旧授权失效：同一 scope 的规则改成 `deny`，授权不起作用。
6. 断线重连保持现有行为：`test_detached_approval_blocks_then_resolves_on_reattach` 继续通过，并且重发的卡片带 `reason` / `scope`。
7. 已验证的空子关掉：`git push origin main --force` 被 `flags=("--force",)` 拒绝；`env git push --force` 同样拒绝；`FOO=1 git status`、`env git status` 是 `ask` 而不是 `allow`；`python -m pip install pytest` 不被 `python -m pytest` 放行。
8. 拒绝文案按来源：handler / 超时 / 取消 / 无消费者四种都不写成 "Denied by the user"。
9. 授权上下文：同一聊天换根目录后旧授权不生效、切回后生效；`remember: true` 的请求在 `resolve` 之前已落库（用并发的第二条同范围调用验证）；落库期间触发超时或取消，授权仍落库、运行仍停止、请求不停在“处理中”。
10. `permission` 谓词返回 bool：调用被拒、有 `ToolCallFailed` 事件，不会静默放行。
11. 批量遍历不泄露 `ask` 文件：`PathRule(".env*", "ask")` 下 `grep(include_hidden=True)`、嵌套目录、符号链接三种情形都不返回其内容；`read_file(".env")` 仍然问。
12. 带 `flags` 的规则各有各的 scope：记住 `git push --tags` 不放开 `git push --delete`。
13. 大小写：`git branch -d` 的 allow 不放行 `git branch -D`。

## 待决问题

1. **`needs_approval` → `permission` 改名**：推荐改，不留兼容名（项目既定）。替代是保留旧名、只放宽返回类型，省掉约 15 处改动，代价是 `needs_approval` 返回 `"deny"` 的别扭。
2. **`coding` 灰区 ask 还是 allow**：推荐 ask。绿名单已收窄，灰区保守才有意义。
3. **`DEV_TOOLS` 是否进 `coding` 默认**：推荐进，文档如实说明“信任项目代码在宿主执行”。替代是只放 `READ_ONLY`，`pytest` 每次问——那又回到弹窗风暴，`DEV_TOOLS` 恰是 kits 实测里最常见的无谓确认。
4. **`trusted` 要不要红名单**：推荐不要。baseline 里受保护路径的写入每次都问、不可记忆，文档说清。
5. **理由的语言**：内置理由是英文（模型要读，核心文档是英文）。授权匹配不依赖理由文本，将来要翻译可按规则 pattern 映射，现在不扩 i18n。

## 附录：`DEFAULT_BASELINE`（起点，评审时定）

```python
# 黑：不可撤回且几乎没有正当理由由 agent 执行。pattern 是前缀，flags= 里的旗标在任意位置都算；区分大小写。
# cmd.exe 条目放 DANGEROUS_COMMANDS_CMD，预设按平台拼。
DANGEROUS_COMMANDS = (
    CommandRule("git push", "deny", "Force-pushes rewrite remote history.", flags=("--force",)),
    CommandRule("git push", "deny", "Force-pushes rewrite remote history.", flags=("-f",)),
    *(CommandRule(f"rm {f} {t}", "deny", "Deletes the filesystem root or the home directory.")
      for f in ("-rf", "-fr", "-r -f", "-f -r") for t in ("/", "/*", "~", "~/")),
    CommandRule("mkfs", "deny", "Formats a disk."),
    CommandRule("shutdown", "deny", "Shuts the host down."),
    CommandRule("reboot", "deny", "Reboots the host."),
)
DANGEROUS_COMMANDS_CMD = (
    CommandRule("shutdown", "deny", "Shuts the host down."),
    CommandRule("format", "deny", "Formats a disk."),
    CommandRule("diskpart", "deny", "Repartitions disks."),
    CommandRule(r"rd /s /q C:\\", "deny", "Deletes the system drive."),
    CommandRule(r"rd /s /q \\", "deny", "Deletes the current drive root."),
)

# 密钥：读写都拒。~ 在 Windows 上展开到 %USERPROFILE%，同一份规则两边都对。
CREDENTIAL_PATHS = tuple(
    PathRule(p, "deny", reason="Credential store.")
    for p in ("~/.ssh", "~/.aws", "~/.gnupg", "~/.netrc", "~/.config/gh", "*.pem", "id_rsa*")
)

# 受保护配置：写要问。grant=False：放开 .git 的写入不该是一个勾选框的事。
PROTECTED_CONFIG = tuple(
    PathRule(p, "ask", ops={"write"}, grant=False, reason=f"Writes to {p} change how tools behave.")
    for p in (".git", ".github/workflows", ".vscode", ".husky", ".lovia")
)

DEFAULT_BASELINE = DANGEROUS_COMMANDS + CREDENTIAL_PATHS + PROTECTED_CONFIG
```

`ENV_FILES`（`**/.env*` 读要问）也在这里（见 baseline 一节）。`rm` 的 16 条枚举看着笨，但它说的就是“拦这些写法”，比一条看似万能、实际看不见 `rm -- -rf /` 的规则诚实。v1 里的裸 `sh` / `bash` 拒绝删掉了：前缀规则表达不了“没有参数”，而 `curl x | sh` 里的 `curl` 本身就在灰区，复合命令取最严格，整条照样会问。`bash x.sh`、`bash -c ...`、`powershell -Command ...`、`cmd /c ...` 都在灰区，灰区不生成 scope，不提供“总是允许”。

清单里每一条都要有 `test_policy.py` 的用例：命中、不命中、被 `baseline=()` 关掉。
