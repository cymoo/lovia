# 生产部署

生产就绪主要取决于 Agent 周围的边界：谁可以调用它、它可以访问什么、最多消耗多少资源，
以及进程崩溃后哪些状态能够恢复。在将 lovia 应用开放给单个可信用户之外的人群前，请逐项
确认以下内容。

## 部署清单

| 边界 | 生产环境选择 |
| --- | --- |
| 身份认证 | 在 Web/API 服务前接入认证网关；lovia 本身不提供认证 |
| 网络 | 除非前方已有受保护的代理，否则保持默认的 Loopback 绑定 |
| Workspace | 从 `readonly` 或关闭状态开始；本地可写 Workspace 等同于宿主机代码执行能力 |
| 密钥 | 只传入必要的环境变量；Workspace Shell 默认使用最小环境，除非设置 `inherit_env=True` |
| 资源限制 | 设置 `max_turns`、`RunBudget`、Provider 超时、Tool 超时和输出上限 |
| 高风险操作 | 写入、外部副作用、定时任务和特权调用应要求人工审批 |
| 持久化 | Session 保存对话，Checkpointer 恢复进行中的 Run，并备份相应存储 |
| 可观测性 | 记录终止事件、失败、耗时、Token 用量和审批决定 |
| 并发 | 内置 Web 服务保持单 Worker；托管 Run 和审批状态属于进程内状态 |
| TLS 与代理 | 显式配置 CA Bundle 和代理信任；生产环境不要关闭证书校验 |

## 保守的起始配置

```python
from lovia import Agent, RunBudget
from lovia.workspace import Workspace

agent = Agent(
    name="service-agent",
    model="<model>",
    workspace=Workspace.local("./data", mode="readonly"),
    default_tool_timeout=30,
    max_tool_output_chars=50_000,
)

budget = RunBudget(max_total_tokens=100_000, max_tool_calls=20)
```

在应用边界传入预算和持久化配置：

```python
result = await agent.run(
    "分析最新报告。",
    max_turns=12,
    budget=budget,
    session=session,
    session_id=user_conversation_id,
    checkpoint=checkpoint,
)
```

具体上限取决于工作负载；关键在于流量到来前就明确设置边界。

## 安全地提供服务

!!! danger "内置服务不提供身份认证"

    `lovia web` 和 `create_app()` 信任所有请求。请绑定 Loopback，或部署在带认证和限流的
    反向代理之后。公网服务一旦结合可写 Workspace，就等同于以服务端用户身份远程执行代码。

内置服务按单进程设计。SQLite 数据可以持久化，但正在运行的 Run、审批、SSE 订阅者和定时任务
协调状态都保存在进程内。请使用 `workers=1`；需要扩展时，应运行相互隔离的应用实例，并明确
设计路由和存储所有权。

## 失败与恢复

- 使用 `RetryPolicy` 在 Run 内重试 Provider 的瞬时故障。
- Worker 可能在 Run 中途重启时，使用 Checkpoint。
- Session 用于保存已经完成的对话；不要把完成过的 `run_id` 用于下一条用户消息。
- 崩溃恢复下，Tool 副作用应按“至少一次”处理，除非 Tool 使用 `ctx.run_id` 和调用参数实现幂等。
- 备份 SQLite 或自定义 Store，并在依赖它们前实际演练恢复流程。

## 延伸阅读

- [Provider 重试](retries.md)、[预算](budgets.md)与[取消](cancellation.md)
- [Session 与 Checkpoint](sessions-and-checkpoints.md)：持久化和幂等
- [工作区](workspace.md)：ACL 与 Shell 安全边界
- [Web 服务端](web-server.md)：服务生命周期和配置
- [可观测性](observability.md)：Hook、Tracing、日志和用量
