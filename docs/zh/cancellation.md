# 取消与运行中引导

应用可以通过两个通道控制正在运行的 Run：`CancelToken` 停止工作，`Mailbox` 则在下一个 Turn
边界追加新的用户指令。

## 取消

```python
from lovia import Agent, CancelToken, Runner

agent = Agent(name="analyst", model="<model>")
token = CancelToken()
handle = Runner.stream(agent, "执行一项较长的分析。", cancel_token=token)

# 可从事件处理器、请求处理器或另一个协程调用：
token.cancel("用户点击停止")
# 已持有 handle 时等价于：handle.cancel("用户点击停止")
```

Run 会在下一个安全点抛出 `RunCancelled`，事件流以 `RunFailed` 结束。如果在一批 Tool 调用
中取消，仍在运行的异步并发调用也会被取消。

即使调用方没有传入 Token，`ctx.cancel_token` 也始终存在，因此 Hook 和 Tool 可以停止自己
所在的 Run。Agent-as-tool 子 Run 会继承父 Token，一次取消可以停止整棵调用树。

取消无法中断同步 Tool 的工作线程，也无法撤回已经发给 Provider 的请求。同步线程可能在 Run
结束后继续完成，其副作用仍可能发生。

## 在运行中追加指令

用户在 Agent 工作过程中补充要求时，使用 `Mailbox`：

```python
from lovia import Agent, Mailbox, Runner

agent = Agent(name="analyst", model="<model>")
mailbox = Mailbox()
handle = Runner.stream(agent, "分析这些日志。", mailbox=mailbox)

mailbox.push("重点检查 14:00 左右的 5xx 峰值。")
result = await handle
```

Runner 在每个 Turn 开始时清空队列，并把每条内容作为普通用户消息持久化。`push()` 不会打断
当前正在进行的模型请求或 Tool 阶段。

| 操作 | 效果 |
| --- | --- |
| `token = mailbox.push(content)` | 把内容加入下一次 drain |
| `mailbox.remove(token)` | 撤回尚未 drain 的内容 |
| `ctx.mailbox.push(content)` | 从 Tool 或 Hook 内追加指令 |

`TurnStarted` Hook 紧接在该 Turn 的 drain 之前执行，因此 Hook 中 push 的内容会进入同一 Turn；
其他位置 push 的内容进入下一 Turn。每条取出的消息都会发出
[`UserMessageInjected`](streaming.md#模型输出)。

调用方提供的 Mailbox 在 Run 结束后仍可交给下一次 Run；Runner 自动创建的默认 Mailbox 则无法
再次访问。Agent-as-tool 子 Run 会获得自己的 Mailbox。

## 延伸阅读

- [预算与限制](budgets.md)：达到资源上限时自动停止
- [流式输出](streaming.md)：观察取消和注入消息
- 示例：[`14_reliability.py`](../../examples/14_reliability.py)、
  [`16_steering.py`](../../examples/16_steering.py)
