# lovia 示例

[English](./README.md)

每个文件都是自包含、可直接运行的小脚本，对应一个功能。按顺序读完就是一遍
框架之旅；任何一个文件复制出去就是一个起点。

## 准备

```bash
pip install -e ".[examples,web]"     # 在仓库根目录执行
cp .env.example .env                 # 然后设置 LOVIA_MODEL 和你的 API key
python examples/01_hello.py
```

`LOVIA_MODEL` 决定所有示例使用的模型，例如 `glm-5.2` 或
`anthropic:<model>`。把 `OPENAI_BASE_URL` 指向任意 OpenAI 兼容服务
（DeepSeek、Ollama、vLLM 等），模型名直接写服务提供的裸名即可。
需要写文件的脚本一律写入 `tmp/`（已被 gitignore）。

两个示例完全离线、无需 key：`10_custom_provider.py` 和 `28_eval.py`；
`19_workspace.py` 也不需要模型。

## 学习路径

### 基础

| 文件 | 展示内容 |
| --- | --- |
| `01_hello.py` | 最小 agent，一次模型调用 |
| `02_tools.py` | `@tool` 函数：类型化 schema、同步/异步、错误语义 |
| `03_streaming.py` | 消费类型化事件流 |
| `04_structured_output.py` | 校验后的结构化输出、按调用覆盖 `output_type` |
| `05_sessions.py` | 用 `SQLiteSession` 持久化多轮对话 |
| `06_multimodal.py` | 通过 `ImagePart` 输入图片 |

### 多 agent

| 文件 | 展示内容 |
| --- | --- |
| `07_handoff.py` | 移交控制权给专家 agent(可用 `Handoff` 定制) |
| `08_agent_as_tool.py` | 把有边界的子任务委派给子 agent |

### 模型与 provider

| 文件 | 展示内容 |
| --- | --- |
| `09_model_settings.py` | `ModelSettings`、`provider_options`、OpenAI 兼容端点 |
| `10_custom_provider.py` | 实现 `Provider` 协议(离线可跑) |

### 控制与生产

| 文件 | 展示内容 |
| --- | --- |
| `11_hooks.py` | 用 `AgentHooks` 观察运行中的每个事件 |
| `12_approval.py` | 人类审批，谓词决定哪些调用需要审 |
| `13_guardrails.py` | 输入/输出护栏 |
| `14_reliability.py` | 预算、provider 与工具重试、超时、取消、fallback |
| `15_resume.py` | 给运行做检查点，中途杀掉，再恢复 |
| `16_steering.py` | 向运行中的 run 注入用户消息(`Mailbox`) |
| `17_context_compaction.py` | 让长对话在上下文窗口内存活 |
| `18_dependencies.py` | 按 run 注入依赖到指令和工具(`RunContext`) |

### 工作区与插件

| 文件 | 展示内容 |
| --- | --- |
| `19_workspace.py` | 把工作区当作普通库使用(无 agent) |
| `20_workspace_agent.py` | 带文件/shell 工具与命令策略的编码 agent |
| `21_todos.py` | `Todo` 插件：外化计划、每轮提醒 |
| `22_skills.py` | 可复用的 skill 指令包与渐进式披露 |
| `23_memory.py` | 跨 run 的长期记忆(`Memory` 插件) |
| `24_mcp.py` | 来自 MCP server 的工具 |
| `25_custom_plugin.py` | 自己写插件(工具 + 注入器 + 清理) |

### 服务与应用

| 文件 | 展示内容 |
| --- | --- |
| `26_web_serve.py` | 内置 HTTP 聊天 UI(`pip install "lovia[web]"`) |
| `27_web_api.py` | 仅 JSON + SSE API，前端自建 |
| `28_eval.py` | 离线评测：检查项、LLM 评审、基线对比 |
| `29_data_analysis.py` | 基于 SQLite 的数据分析 agent + 图表报告 |
| `30_support_bot.py` | 综合练习：交互式终端客服 bot |

## 子目录

- [`tools/`](tools/) — 每个内置工具族一个脚本(HTTP、时间、搜索、问人)。
- [`workflows/`](workflows/) — Anthropic《Building effective agents》中的各
  模式(chaining、routing、parallelization、orchestrator、evaluator loop、
  自主 agent)的纯 Python 实现。
- [`skills/`](skills/) — `22_skills.py` 使用的示例 skill 目录。
