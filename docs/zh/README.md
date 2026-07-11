# lovia 中文文档

初次接触 lovia，不必从头读完整套文档。先运行一个 Agent，再根据手头的任务查阅对应章节即可。

## 从这里开始

- **想先跑起来**：[快速上手](quickstart.md)
- **想直接看代码**：[示例](../../examples/README-zh.md)
- **想理解运行机制**：[核心概念](concepts.md)

## 按任务查阅

| 我想做什么 | 看这里 |
| --- | --- |
| 定义 agent、编写 instructions、派生变体 | [Agent](agents.md) |
| 运行 agent，处理输入、结果和错误 | [运行 agent](running.md) |
| 做流式 UI 或消费运行事件 | [流式输出](streaming.md) |
| 让模型调用 Python 函数 | [工具](tools.md) |
| 使用 HTTP、搜索、时间等内置工具 | [内置工具](built-in-tools.md) |
| 让最终答案变成 Pydantic 对象或 JSON | [结构化输出](structured-output.md) |
| 配置模型、OpenAI 兼容端点、自定义 provider | [Provider 与模型](providers.md) |
| 组合多个 agent，做 handoff 或 agent-as-tool | [多 Agent](multi-agent.md) |
| 打包可复用能力 | [插件](plugins.md) |
| 加载团队知识、runbook、风格指南 | [技能](skills.md) |
| 接入 MCP 服务器工具 | [MCP](mcp.md) |
| 做跨会话长期记忆 | [记忆](memory.md) |
| 保存多轮对话，支持崩溃恢复和幂等运行 | [Session 与 Checkpoint](sessions-and-checkpoints.md) |
| 控制长上下文和压缩策略 | [上下文管理](context.md) |
| 给危险工具加人工审批 | [人工介入](human-in-the-loop.md) |
| 在运行前后加安全检查 | [护栏](guardrails.md) |
| 设置重试、预算、取消 | [可靠性](reliability.md) |
| 让 agent 访问文件和 shell | [工作区](workspace.md) |
| 启动聊天 UI 或服务端 | [Web UI 与服务端](web.md) |
| 接自己的前端或服务 | [HTTP API](http-api.md) |
| 看日志、事件、trace 和 token 用量 | [可观测性](observability.md) |
| 写确定性的离线测试 | [测试](testing.md) |
| 做行为评测和基线对比 | [评测](eval.md) |

## 内部机制

[架构笔记](../architecture.md)记录了框架本身的构造方式：模块地图、runner 内部机制和
关键不变量。它主要面向贡献者；如果你想理解某个设计为什么这样取舍，也值得一读。

---

英文版：[docs/en](../en/README.md)。
