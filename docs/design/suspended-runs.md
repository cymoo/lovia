# 无人值守时的审批：暂停，批准后继续

> 状态：已实施（#476 core、#477 web、#478 子任务与关闭服务、本 PR `ask_human`）。实施时与本稿不同的两处：SSE 帧最终叫 `suspended`（审批卡片、问题和工具共用），`waiting_for` 多了 `kind`（`approval` 或 `call`）。五项决定见文末"已定"；评审补的四处缺口在 §2–§6，标"评审补充" · 2026-11-17 · 范围：core（`exceptions.py`、`events.py`、`approvals.py`、`runtime/tool_calls.py`、`runtime/loop.py`、`runtime/checkpoint.py`）、`lovia/web`（`supervisor.py`、`approvals.py`、`questions.py`、`store.py`、`scheduler.py`、`subagents.py`、`api/chat.py`、`api/sessions.py`、`schemas.py`、`static/js/approvals.js`、`sessions.js`）。session engine 不改。
>
> 行号指向 `main`（`84fc0682`，v0.9.229）。

## 结论

core 加一个信号 `RunSuspended`：run 停在一个调用上，等外面的事。checkpoint 保留，同一个 `run_id` 续跑时会重新执行这个调用。web 层用它替换"等 10 分钟后自动拒绝"。

用户看到的变化：

- 审批卡片**不会过期**。什么时候批准，回复就从那里继续。
- 等待中的回复**不占运行槽位**，定时任务和子任务不会因为一个没人看的审批被堵住。
- **重启后卡片还在**。
- 批准、拒绝、发消息、停止，操作和现在一样。用户不需要知道"暂停"这一步。

| # | PR | 范围 |
| --- | --- | --- |
| 1 | core：`RunSuspended` 和 `ApprovalRequired.suspend()` | core，约 40 行 |
| 2 | web：等待超时改为暂停，回答后继续，状态存储，卡片和侧栏 | `web/` |
| 3 | web：定时任务、子任务、关闭服务时暂停、文档 | `web/`、两份 README、`docs/{en,zh}` |
| 4 | 第二阶段：`ask_human` 也能暂停 | core 工具执行、`web/questions.py` |

不加依赖，不加表。core 不加新的 RunStatus 和事件类型。

## 背景

"Building always-on agents"（claude.com，2026-11-13）把无人值守运行的审批称作 *deferred tool call*：run 结束，会话存档，请求发给用户；用户的回答作为新事件回来，续跑存档的会话；这段时间 worker 去处理别的事。

lovia 现在的做法正好相反：

| 现状 | 位置 |
| --- | --- |
| 等审批的 run 在进程里原地等，一直占着并发槽位 | `supervisor.py:967`–`1006` |
| `lovia web` 600 秒后自动拒绝。模型收到"用户没在 10 分钟内回答"，只能绕路 | `default.py:34`、`supervisor.py:610` |
| `create_app` 默认 `approval_timeout=None`：一直等，槽位一直占着 | `app.py:200`；`_await_approval` 的 docstring 写明了这个风险 |
| `ask_human` 也一样，`question_timeout` 到点让工具调用失败 | `questions.py:14`–`17` |
| 等待期间进程重启：run 记为 interrupted，打开对话时 `reconnect` **自动续跑**并重新询问 | `api/chat.py:455` |
| 定时任务的设计稿已经把"10 分钟自动拒绝，表单里没有提示"列为缺口 | `docs/design/web-schedules.md` §1 |

需要的零件 core 基本都有：

- 执行工具前先存 checkpoint（`loop.py:440`）。
- 续跑时补跑没有结果的调用，并重新走审批（`loop.py:814`）。`tool_calls.py:300`–`304` 的注释已经写着"the call stays dangling for a resume to re-ask"。
- `BudgetExceeded` 已经有"不再派发、等已开始的调用结束并存档、然后退出"的处理（`loop.py:1081`）。

缺的只是一个出口，表示"现在没人能回答，先停下"。

## 1. core：`RunSuspended`

```python
class RunSuspended(LoviaError):
    """The run stopped at a call to wait for something outside it.

    The checkpoint is kept; resuming the same run_id re-runs that call."""
    reason: str
    call: ToolCall | None   # filled in by the runner
```

暂停审批的动词和 approve/reject 放在一起：

```python
ev.suspend(reason="")                             # ApprovalRequired
channel.suspend(call_id, scope=None, reason="")   # ApprovalChannel
```

语义：

1. **在哪里抛出。** preflight 发现审批被暂停，就抛出 `RunSuspended(call=...)`（`tool_calls.py:294` 之后）。这次调用不写结果，留作悬空调用。
2. **在跑的调用怎么办。** `_tool_phase` 用处理 `BudgetExceeded` 的方式处理它：不再派发后面的调用，等已开始的并行调用跑完并存档，然后抛出。改动就是 `loop.py:1081` 的 `except BudgetExceeded` 改成 `except (BudgetExceeded, RunSuspended)`，变量 `budget_abort` 改名为 `abort`。

   为什么不直接取消：`Tool.parallel` 默认是 `True`（`tools/base.py:144`）。同一轮里已经允许的 `npm test` 可能正在跑，取消它，续跑时就得重跑一遍。
3. **checkpoint。** `classify` 把它归为 `interrupted`（`checkpoint.py:137`）。不新增 RunStatus：续跑路径只认 `interrupted`/`running`，至于为什么停下，是服务层的事。
4. **事件。** 发出 `RunFailed(error=RunSuspended)`，和 `BudgetExceeded`、`MaxTurnsExceeded` 一样，不加新的事件类型。
5. **续跑。** 悬空调用重新判定（`tool.decide`）。模式已经放宽，就直接执行；已经收紧，就拒绝；仍然是 `ask`，就再发一次 `ApprovalRequired`，由调用方拿自己存的答案回答。**core 不存答案。**
6. **`approval_handler`** 里也可以调用 `request.suspend()`。不另加返回值 token：`"ask"` 已经表示"交给流的消费方，没人答就拒绝"（`tool_calls.py:503`）。

不用 web 的部署方（Slack 机器人、队列 worker）这样用，下面就是全部代码：

```python
answers: dict[str, Approval] = {}            # 你自己的存储

def handler(req, ctx):
    if (a := answers.pop(req.call.id, None)) is not None:
        return a
    req.suspend("waiting for the user in Slack")

agent = agent.clone(approval_handler=handler)
ckpt = CheckpointOptions(checkpointer=store, run_id=run_id)
try:
    result = await Runner.run(agent, text, checkpoint=ckpt)
except RunSuspended as s:
    await post_card(s.call)                  # 发卡片，这次处理到此结束

# 之后用户点了"允许"：
answers[call_id] = Approval(True)
result = await Runner.run(agent, [], checkpoint=ckpt)   # 同一个 run_id，接着跑
```

## 2. web：什么时候暂停

用 `suspend_after: float | None` 同时替换 `approval_timeout` 和 `question_timeout`：等待超过这么多秒没人回答，就暂停。1.0 之前直接改名，不留别名。

- `create_app` 默认 `None`：一直在线等，和现在一样，服务层不擅自改。
- `lovia web`（`default_app`）默认 60 秒。
- 到点时 `ApprovalRegistry.suspend_pending(sid)` 调用 `ev.suspend()`，run 照 §1 的方式结束。
- 不区分有没有标签页在看，规则只有一条。用户在场，60 秒内多半已经回答；不在场，就把槽位放出来。
- **关闭服务时**（`supervisor.py:1571`）：正在等审批的**顶层对话**先暂停，再退出，所以重启后卡片还在。子任务照旧停止，理由见 §6。其余 run 照旧记为 interrupted。进程崩溃时退回现在的行为。
- 去掉自动拒绝，见"不做"。

## 3. web：暂停状态存在哪

- `chat_runs` 记录的 `status` 加一个值 `waiting`。新增一列 `waiting_for`（JSON，可空）：`{"kind": "approval", "request": <approval_required frame>}`。迁移一步。`kind` 给第二阶段的 `"question"` 留位置。
- checkpoint 不变，`active_run_id` 仍然指向它。
- 卡片要的 reason、scopes、参数都在 frame 里，所以显示卡片不需要续跑，也不需要重新判定。
- 不写进 checkpoint 的 meta：不把 web 的概念放进 core 的快照。
- **待读队列**（评审补充）：`waiting_for` 里还有一个 `queued` 列表，续跑时按顺序放回邮箱。里面有两类内容：
  - 暂停那一刻邮箱里还没读到的内容。现在 run 没有成功结束时，剩下的内容会被直接丢掉（`supervisor.py:1042`），暂停必须例外。否则用户发了消息（§5）却仍然不批准，再次暂停时这条消息就丢了。
  - 等待期间送来的子任务报告（§5）。
- **续跑用的 agent**（评审补充）：supervisor 在内存里为每个等待中的对话记下当时用的 agent（`ctrl.source_agent`）。顶层对话丢了这份记录也没关系，可以按注册名重新找到；子任务不行，见 §6。

## 4. web：回答后继续

`POST /api/chat/approve` 的请求体不变。找不到在线的待批请求时：

1. 读对话当前 run 的记录。如果 `status = 'waiting'`，并且 `call_id` 对得上，就用一条 `UPDATE … WHERE status = 'waiting'` 把它改成 `running`，这一步就是 claim。第二个标签页拿到 404 `approval_not_found`，和现在一样。
2. 调用 `start_resume(..., answers={call_id: Approval(...)}, autostart=True)`。如果勾了"始终允许"，controller 在开始之前写入 grant。
3. 返回 `{ok, remembered, resumed: true}`。客户端要看进度，就照现有逻辑 reconnect，附着到 run 上。

**评审补充：**

- **批准后要自己开始跑。** 现在 `start_resume()` 只注册 controller，任务在第一次订阅时才启动（`_ensure_begun`，`supervisor.py:404`），而 `attach()` 不会触发它（`supervisor.py:445`）。所以 `start_resume()` 要像 `start()` 一样加上 `autostart`（`supervisor.py:1346`），批准接口返回之前就开始续跑。从托盘、定时任务页、部署方自己的 UI 批准，都不依赖有人重连。
- **用哪个 agent 续跑。** 先用内存里记下的那个（§3），没有再按注册名找。都找不到时返回 409 `agent_unregistered` 并附上提示，**不删 checkpoint**。现在的续跑路径在这种情况下会删掉它（`api/chat.py:486`）。

supervisor 这边，`RunController` 加一个 `_answers: dict[str, Approval]`。`ApprovalRequired` 分支（`supervisor.py:967`）先查它，命中就 approve/reject，发出 `ApprovalResolved(cause="user")`，用一次就丢。

- **拒绝也会续跑**：模型拿到拒绝理由接着做，和在线拒绝一样。
- **续跑会重新判定**：等待期间切换了模式，结果也是对的。#457 留下的"已停在审批上的调用不会重判"，在这里自然解决了。

## 5. web：对话里的其他操作

**一条总规则**（评审补充）：对话里有等待中的 run 时，`supervisor.start()` 一律返回 409 `run_waiting`。新开 run 会把 `active_run_id` 改指向自己（`_checkpoint_for`，`supervisor.py:1235`），暂停的 run 就再也找不回来了。最初的设计只照顾了"用户发消息"和"定时任务"两种启动方，漏了子任务报告（`web/subagents.py:155`）。现在改成 `start()` 本身拒绝，每个启动方都必须明确处理这种情况，以后再加启动方也不会悄悄覆盖暂停的 run。

| 操作 | 行为 |
| --- | --- |
| 打开对话或刷新 | 会话视图返回 `waiting`（frame），不返回 `active_run_id`，所以不自动续跑，客户端直接画卡片。`reconnect` 遇到 waiting 返回 409 `run_waiting` 兜底 |
| 发新消息 | 续跑，并把这条消息注入进去，和在线等待时发消息一样：消息进 mailbox，卡片还在。现在 `api/chat.py:209`、`321` 会删掉被新消息取代的 checkpoint，这一条对 waiting 不适用，暂停的工作不会丢 |
| 子任务的报告送到（评审补充） | 放进待读队列（§3），**不续跑**。审批只能由用户来答，为了一份报告续跑，60 秒后又会暂停，没有意义。用户批准后，run 在下一轮读到这份报告。走的是 `subagent_deliver` 碰到 `run_waiting` 的分支 |
| 停止 | 和停止运行中的回复一样：保留已经发生的内容，悬空调用记为已取消，删除 checkpoint，记录为 cancelled。要把 `_write_frozen`（`supervisor.py:697`）抽成可以直接从快照调用的函数 |
| 切换权限模式 | 存下新模式，然后续跑一次，让 core 重新判定。新模式允许的就直接执行，否则重新出卡片。这和在线 run 的 `recheck_pending` 是同一个语义 |
| 删除对话 | 现有的删除门已经覆盖 checkpoint，不用改 |

## 6. 定时任务和子任务

**定时任务**（`scheduler.py:236`–`262`）：

- 现在的规则是"上一次还没结束就跳过"。waiting 也算没结束，跳过原因写"上一次运行在等你批准"。
- 续写固定对话的任务，目标对话在等待时同样跳过，因为没有在线的 run 可以注入。
- 理由：一个每天跑的任务如果每天卡在同一个审批上，不应该每天多出一个等待中的对话。
- 任务页的历史行显示"等待批准"，点开就是那个对话。表单里关于自动拒绝的说明可以删掉。

**子任务**（`subagents.py:386`–`416`）：现在 `run_child` 等子任务的 run 结束。子任务一暂停，就会被当成失败报给父对话。改法：

- supervisor 加一个 `settled(session_id)`，在暂停和续跑之间一直等，直到这个对话的 run 以非 waiting 的状态结束。`run_child` 改为等它。
- 父对话的托盘（#459）从 `/api/runs` 读取，所以暂停的子任务审批照样显示，见 §7。
- 进程重启后，父对话内存里的子任务列表本来就会丢，这是现有限制，这次不处理。

**子任务续跑用的 agent**（评审补充）：

- 子 agent 是一个临时克隆，名字是 `{parent}-sub`，`plugins=[]`，没有注册（`plugins/subagents.py:402`）。任务对话存的却是父 agent 的注册名（`web/subagents.py:265`）。所以按注册名找不到它；换成父 agent 来续跑也不行，过不了 `resolve_resume_agent` 的校验（`runtime/resume.py:133`）。
- 子任务等待期间，`run_child` 一直在等 `settled()`，进程也活着，所以内存里记下的 agent（§3）一定还在。续跑就用它。
- 也正因为这样，子任务的暂停只在进程活着时有效。关闭服务时子任务照旧停止，不暂停（§2），这和文档里现有的"未完成的任务重启后不会续跑"一致。
- 如果万一出现一个找不到 agent 的等待中任务，按 §4 返回 409，不删 checkpoint。

**父对话在等审批时，子任务完成了：** 报告进父对话的待读队列（§5），父对话里暂停的 run 和卡片都不受影响。用户批准后，父对话从暂停处继续，并在下一轮读到这份报告。

## 7. UX

- **卡片**：样子和在线时一样。去掉"N 分钟后自动拒绝"那一行（`approvals.js`）；暂停后，标题下多一行灰字"已暂停 · 批准后继续"。
- **侧栏和托盘**：`/api/runs` 也返回等待中的对话。`RunInfo.status` 加 `"waiting"`，`pending_approval` 从记录里取。"需审批"的点（`sessions.js:534`）对两种状态一样显示。托盘、侧栏、部署方自己的 UI 都只有一个数据来源，不用分情况处理。
- **定时任务页**：见 §6。
- **重启**：正常关闭的，卡片还在；崩溃的，退回现在的 interrupted 行为。
- 浏览器通知和隐藏标签页的提示（#351）不在这次范围内。

## 8. DX

- **一个参数替换两个**：`suspend_after` 替换 `approval_timeout` 和 `question_timeout`。
- **推送到 Slack 或邮件**：不加新钩子，插件的 `hooks` 已经能拿到：

  ```python
  hooks = AgentHooks()

  @hooks.on(events.RunFailed)
  async def notify(ev, ctx):
      if isinstance(ev.error, RunSuspended):
          await slack.post(f"{ev.error.call.name} 等你批准：{chat_url(ctx.session_id)}")
  ```

  这段放进 `docs/{en,zh}/web-ui.md` 作为示例。
- **core 用户**：用法见 §1 的示例。

## 9. 第二阶段：`ask_human`

- 工具也可以抛 `RunSuspended`。工具执行像对待 `RunCancelled` 一样让它向上传（`tool_calls.py:355`、`412`）。并行工具抛出时，`_drain_batch` 先记下来，不立即重抛，按 §1 第 2 条处理。
- `ask_human` 收到暂停就抛出。续跑时 `ask_human` 会重跑一次，`QuestionRegistry` 用存下的回答立即应答，按 call id 对应。
- `waiting_for` 的 kind 用 `"question"`，`POST /api/chat/answer` 按 §4 处理。
- 这样一来，部署方自己的工具，比如"等 CI 跑完"，也能暂停 run，再由 webhook 续跑，这就是文章里的 inbox 模式。这里只是说明它能做到，不在计划里。

## 不做和暂缓

- **到点自动拒绝的选项**：它当初只是为了放出槽位，暂停已经解决了这件事。
- **新的 RunStatus 或事件类型**：理由见 §1 第 3、4 条。
- **持久化的 inbox 和队列 worker**：supervisor 有意做成单进程，steering 已经能把运行中收到的消息并进当前 run。
- **内置浏览器以外的推送渠道**：用 §8 的示例。
- **重启后自动续跑被中断的无人值守 run**：会重跑执行到一半的工具。
- **进程崩溃时保住等待中的审批**：要做到就得每次审批都写库。正常关闭的情况 §2 已经覆盖。

## 已定（2026-11-17）

1. **`lovia web` 的 `suspend_after` 默认 60 秒。** 没有选 0（立即暂停），因为用户在场时，在线等待比续跑更快；也没有沿用 600，因为暂停之后再回答没有任何损失，不需要等那么久。
2. **子任务第一阶段也暂停**（§6）。否则去掉自动拒绝后，子任务的审批只能一直在线等，槽位也一直占着。
3. **切换权限模式时，续跑等待中的对话**（§5），和在线 run 的 `recheck_pending` 一致。
4. **往等待中的对话发新消息：续跑并注入**（§5），和在线等待时发消息的行为一致。不当作拒绝，因为用户可能只是想补充说明。
5. **沿用 `RunFailed` 事件**（§1 第 4 条），和 `BudgetExceeded`、`MaxTurnsExceeded` 一致。

## 测试

- **core**（用 `ScriptedProvider`）：
  - preflight 暂停时，正在跑的并行调用跑完，结果已存档；暂停的调用和它后面的调用都是悬空的。
  - 续跑时 handler 给出答案，调用恰好执行一次。
  - 续跑前放宽了模式：不再询问，直接执行。
  - `approval_handler` 里调用 `req.suspend()`。
  - `classify` 返回 `interrupted`。
- **web**：
  - 暂停后批准会续跑，答案只用一次；同时提交两次批准，后一次返回 404。
  - 关闭服务后重启，卡片还在。
  - reconnect 不自动续跑。
  - 发新消息会续跑并注入；停止会保留内容。
  - 切换模式会续跑并重新判定。
  - 定时任务的跳过原因正确。
  - 子任务暂停后，父对话在子任务续跑完成后才拿到结果。
  - 评审补充：
    - 没有任何客户端时批准，run 也会续跑并跑完。
    - 子任务的 agent 没有注册，暂停后批准仍能续跑；找不到 agent 时返回 409，checkpoint 还在。
    - `supervisor.start()` 遇到等待中的对话返回 `run_waiting`。
    - 父对话在等审批时子任务完成：`active_run_id` 不变，报告在待读队列里，批准后 run 读到它。
    - 发了消息但仍不批准、再次暂停：这条消息进了待读队列，续跑后还在。
  - 用 verify skill 和 stub model 在浏览器里走一遍：卡片、侧栏的点、托盘、刷新。

## 合并顺序

1 → 2 → 3 依次堆叠，4 单独做。第 2 个 PR 改了公开参数，要同步两份 README 和 `docs/{en,zh}/web-ui.md`、`web-server.md`、`http-api.md` 里关于 `approval_timeout` 和 `question_timeout` 的段落。每个 PR 照常 bump 补丁版本号。
