# Todo

`Todo` 为多步骤任务提供一份外置清单。模型通过 Tool 更新它，并在该 Tool 的结果里看到
每次更新；清单还有未完成项、又有一段时间没更新时，会再展示给模型。

```python
from lovia import Agent, Runner, Todo

agent = Agent(
    name="builder",
    instructions="谨慎完成多步骤工作。",
    model="<model>",
    plugins=[Todo()],
)

result = await Runner.run(
    agent,
    "实现一个小型 REST API，并编写测试和文档。",
)
```

## 工作方式

插件提供两项能力：

- `todo_write`：采用全量替换语义的 Tool。每次调用都携带完整列表，因此最新一次有效结果
  就是权威状态。
- 过期提醒：列表还有未完成项，且距上次有效写入已过 `remind_every` 次模型调用时，
  View Injector 把当前列表渲染为 `<system-reminder>` 再展示一次；之后只要一直没更新，
  每隔 `remind_every` 次调用再提醒一次。重新写入、或全部项目完成，提醒就停止。计数取自
  Transcript，所以跨 Run 累计：纯聊天每个 Run 只调用一次模型，多个 Run 会累加。提醒不会
  进入 Transcript，不会破坏稳定的提示词前缀，也不会随着运行不断累积。

每个 `TodoItem` 包含 `content`、状态（`pending`、`in_progress` 或 `completed`），以及
可选的 `active_form`。列表最多保留一个 `in_progress` 项，多余的项目会被降级，而不是让
整次更新失败。

## 配置

```python
Todo(
    tool_name="todo_write",
    remind_every=8,
    instructions=None,  # None 表示移除内置使用指导
)
```

设置 `remind_every=None` 可以保留 Tool 但关闭提醒；设为 `1` 则写入之后每次调用都重新展示。
只有当其他 Tool 已占用
`todo_write` 时，才需要修改 `tool_name`。

## 恢复与观察

Todo Store 本身属于 Run，但列表可以跨中断和 Handoff 恢复：插件激活时，会从 Transcript
中最新的有效 `todo_write` 调用重建状态。

宿主应用可以观察 `event.call.name == "todo_write"` 的 `ToolCallCompleted` 事件；
`event.result` 是结构化的 `list[TodoItem]`。对于已经保存的对话，可调用
`lovia.plugins.todos_from_entries(entries)` 重建列表。

内置 Web UI 在输入框上方用一行显示当前步骤，点开是完整列表；只要列表还有未完成项，这一行就一直在
（没有运行时变淡）。每次更新也会作为回复里的一个步骤显示：那一行写明更新后的进度和当前步骤，展开就是更新后的完整列表。

## 使用建议

- Todo 是规划辅助工具，不是定时器或持久化任务队列。未来任务应使用[定时运行](web-server.md#定时任务)。
- 临时提醒有意不进入 Session 和 Checkpoint；需要审计时应查看 `todo_write` Tool 调用。
- 提醒按 Transcript 里的模型调用计数，而纯文本回复与紧接着的无新输入 Run
  （`Runner.run(agent, [], session=...)`）之间没有分隔。这个 Run 的回复如果直接以
  文本开头（前面没有推理），两者会算作一次调用。
- 任务过于简单或模糊时，模型可能频繁改写计划。建议在 Instructions 中明确只为真正的
  多步骤工作使用 Todo。

## 延伸阅读

- [插件](plugins.md)：生命周期和 View Injector
- [流式输出](streaming.md)：观察 Tool 事件
- 示例：[`21_todos.py`](../../examples/21_todos.py)
