# 故障排查

先查看异常类型和 `.hint`。lovia 的异常会尽量明确指出问题来自调用配置、Provider、Tool，
还是运行边界。

交互式排查时，可以开启框架日志：

```python
from lovia import enable_logging

enable_logging("DEBUG")
```

提交问题时，请勿附带 API Key、完整提示词、私有 Tool 结果或环境变量转储。

## 未配置模型

**现象：**第一个 Turn 前抛出 `UserError`，通常是 Agent 没有模型或模型字符串无效。

给 Agent 传入有效的 `model=`，或设置 `LOVIA_MODEL` 并用 `model_from_env()` 读取；两种方式
都需要提供当前端点的凭证和 Base URL。注意 Python 库不会自动加载 `.env`。具体配置见[安装](installation.md#配置模型)。

## Provider 鉴权或端点失败

| 现象 | 检查项 |
| --- | --- |
| HTTP 401/403 | API Key 是否属于当前端点，并已导出到当前进程 |
| HTTP 404 | Base URL 是否包含端点要求的前缀，通常是 `/v1` |
| Anthropic-compatible 端点拒绝请求体 | 模型名是否带 `anthropic:`，服务是否通过 `ANTHROPIC_BASE_URL` 配置 |
| OpenAI-compatible 端点拒绝请求体 | 模型名是否不带前缀或带 `openai:`，服务是否通过 `OPENAI_BASE_URL` 配置 |
| 端点拒绝原生 JSON Schema | 对需要 Prompt Fallback 的 OpenAI-compatible 端点传入 `supports_json_schema=False` |

如果异常提供了 `ProviderError.status_code`、`.vendor`、`.model` 和 `.retryable`，请一并检查。
端点方言规则详见 [Provider 与模型](providers.md)。

## 代理与 TLS

Provider 客户端默认忽略环境中的 `HTTP_PROXY` / `HTTPS_PROXY`。如果确实需要这些代理，
请设置 `LOVIA_PROVIDER_TRUST_ENV=1`。

私有 CA 使用 `LOVIA_HTTP_CA_BUNDLE=/path/to/ca.pem`。安装 Web 可选依赖后，也会使用操作系统
信任存储。`LOVIA_HTTP_INSECURE=1` 会关闭证书校验，只应短暂用于本地诊断，不能用于生产环境。

## 上下文溢出或早期指令消失

- 遇到 `ContextOverflowError`：设置已知的 `context_window`，减少输出预留，或检查自定义压缩阶段。
- Ollama 不会抛出溢出错误，而是静默丢弃最早内容：请配置与 `num_ctx` 一致的
  `Compaction(context_window=...)`。
- Tool 结果过大：从源头缩小输出或降低 `max_tool_output_chars`。压缩只缩小 View，
  不会缩小持久化的 Transcript。

详见[上下文管理](context.md)和 [Provider 上下文窗口](providers.md#上下文窗口)。

## 模型没有调用 Tool

依次检查：

1. Tool 是否已挂载到 Agent，且名称唯一。
2. Docstring 是否说明了**何时调用**，而不只是返回什么。
3. 模型是否支持 Tool Calling，并实际收到了 Tool Schema。
4. Instructions 是否要求模型避免该操作。

离线测试中可以通过 `ScriptedProvider.calls` 检查模型实际收到的 View。如果 Tool 已运行但失败，
普通异常会作为 Tool 结果返回给模型；`RunCancelled` 和 Run 级 `BudgetExceeded` 仍会结束 Run。

## 结构化输出校验失败

尽量保持 Schema 扁平、明确，检查 `OutputValidationError.raw`，并确认端点使用原生 JSON Schema
还是 Prompt Fallback。每次修复都会额外消耗一个 Turn。详见[结构化输出](structured-output.md)。

## Run 重复旧结果或忽略新输入

已经完成的 Checkpoint `run_id` 是幂等键：复用它会重放旧结果，并忽略新输入。新工作应使用
新的 `run_id`，对话连续性则使用稳定的 `session_id`。详见
[Session 与 Checkpoint](sessions-and-checkpoints.md#run_id-是幂等键)。

## 流式运行结束但没有抛异常

这是既定行为。事件迭代以 `RunFailed` 结束；只有 `await handle.result()` 才会抛出异常。
消费完事件流后，务必等待最终结果。

## Web 服务无法访问或不适合暴露

- 默认绑定 `127.0.0.1:8000`，远程机器无法访问 Loopback。
- 内置服务没有身份认证或限流。
- 使用单 Worker；实时 Run 托管和审批状态属于进程内状态。
- 暴露服务前，关闭或严格限制可写 Workspace。

修改绑定地址前，请先阅读[生产部署](deployment.md)。

## 缺少可选依赖

安装对应的 Extra：

```bash
pip install "lovia[mcp]"   # MCP
pip install "lovia[ddg]"   # DuckDuckGo 搜索
pip install "lovia[web]"   # FastAPI 服务端和 UI
```

## 提交有效的问题报告

请包含：

- `lovia.__version__` 和 Python 版本
- 异常类型、消息、`.hint` 和链式原因
- Provider 类型，以及端点是官方还是 Compatible
- 尽可能使用 `ScriptedProvider` 构造最小复现
- 脱敏日志和相关配置项名称，不要提供密钥值
