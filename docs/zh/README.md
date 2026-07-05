# lovia 中文文档

这些指南按功能逐页深入。想快速了解全貌，请看
[中文 README](../../README-zh.md)；想边跑边学，请看
[示例](../../examples/README-zh.md)。这里则是你真正使用时会反复回来查的参考。

## 建议先读

1. **[快速上手](quickstart.md)**：安装、配置模型，并在十分钟左右写出一个
   支持流式输出和工具调用的 agent。
2. **[核心概念](concepts.md)**：先把心智模型立住：一次运行到底做了什么，
   以及后续文档默认你已经知道的五个概念。
3. **[示例](../../examples/README-zh.md)**：三十个按编号排列、彼此独立的脚本。
   顺序阅读是一条学习路径，也可以任意拷一个当起点。

下面的内容按需阅读：需要哪个能力，再打开对应指南。

## 指南

### 核心

日常会用到的表面：定义 agent、运行它们，以及塑造它们能做什么、怎么回答。

| 指南 | 内容 |
| --- | --- |
| [Agent](agents.md) | `Agent` 字段、静态和动态 instructions、`clone()`、每次运行的依赖 |
| [运行 agent](running.md) | `run` / `run_sync` / `stream`、输入（含图片和文件）、`RunResult`、错误 |
| [流式输出](streaming.md) | 类型化事件目录，以及如何基于它构建 UI |
| [工具](tools.md) | `@tool`、schema 推导、并发执行和屏障、重试、策略 |
| [内置工具](built-in-tools.md) | HTTP fetch、Web 搜索和时间工具 |
| [结构化输出](structured-output.md) | `output_type`、校验、自动修复 |
| [Provider 与模型](providers.md) | 模型字符串、OpenAI 兼容端点、fallback 链、自定义 provider、提示词缓存、reasoning 模型 |

### 组合

用小 agent 和可复用能力包组合出更大的行为。

| 指南 | 内容 |
| --- | --- |
| [多 Agent](multi-agent.md) | handoff、agent-as-tool，以及什么时候该用哪一个 |
| [插件](plugins.md) | 唯一扩展轴：插件能贡献什么，以及如何写一个插件 |
| [技能](skills.md) | 带渐进披露的可复用指令包 |
| [MCP](mcp.md) | 来自 Model Context Protocol 服务器的工具 |
| [记忆](memory.md) | 跨会话长期记忆：Notes、Archive 和召回 |

### 生产

接入真实应用时需要的控制点：持久化、控制和限制。

| 指南 | 内容 |
| --- | --- |
| [Session 与 Checkpoint](sessions-and-checkpoints.md) | 多轮历史、崩溃恢复、幂等运行 |
| [上下文管理](context.md) | 压缩、视图/transcript 分离、自定义上下文策略 |
| [人工介入](human-in-the-loop.md) | 各种工具审批方式，以及 `ask_human` |
| [护栏](guardrails.md) | 可以停止运行的输入/输出检查 |
| [可靠性](reliability.md) | 重试、fallback、预算、取消、运行中追加指令 |
| [可观测性](observability.md) | hooks、tracing、日志和用量统计 |
| [工作区](workspace.md) | 受权限策略约束的文件和 shell 工具 |

### 服务与质量

把 agent 放到用户面前，并持续验证它的行为。

| 指南 | 内容 |
| --- | --- |
| [Web UI 与服务端](web.md) | `serve()`、零配置 CLI、后台运行、定时任务 |
| [HTTP API](http-api.md) | JSON + SSE 端点，以及自带前端之外的接入方式 |
| [评测](eval.md) | 带检查项和 LLM 裁判的声明式行为测试套件 |
| [测试](testing.md) | 使用 `ScriptedProvider` 写确定性的离线测试 |

## 内部机制

[架构笔记](../architecture.md)记录了框架本身如何构建：模块地图、runner 内部、
关键不变量。它主要写给贡献者，但当指南里的“为什么”还不够时，也很值得一读。

---

英文版：[docs/en](../en/README.md)。
