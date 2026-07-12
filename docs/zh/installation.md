# 安装

先安装精简核心，再按实际需要加入集成能力。lovia 要求 Python 3.10 或更高版本。

## 安装

```bash
pip install lovia
```

| 能力 | 安装命令 |
| --- | --- |
| Agent、工具、Provider、Session、工作区 | `pip install lovia` |
| MCP 客户端支持 | `pip install "lovia[mcp]"` |
| DuckDuckGo 搜索后端 | `pip install "lovia[ddg]"` |
| FastAPI 服务端、聊天 UI 和定时任务 | `pip install "lovia[web]"` |

多个可选依赖可以组合安装，例如 `pip install "lovia[mcp,web]"`。

## 配置模型

`Agent(model=...)` 接受 Provider 实例或模型字符串。不带前缀的模型名使用 OpenAI-compatible
适配器；带 `anthropic:` 前缀的模型名使用 Anthropic-compatible 适配器。

=== "OpenAI"

    官方端点是默认值，无需设置 Base URL。

    ```bash
    export OPENAI_API_KEY="sk-..."
    export LOVIA_MODEL="<openai-model>"
    ```

=== "Anthropic"

    `anthropic:` 前缀用于选择 Anthropic Messages 适配器。

    ```bash
    export ANTHROPIC_API_KEY="sk-ant-..."
    export LOVIA_MODEL="anthropic:<anthropic-model>"
    ```

=== "OpenAI-compatible"

    DeepSeek、vLLM、LM Studio、模型网关等服务通常提供 OpenAI-compatible 端点。模型名应
    使用该服务实际公布的名称；部分本地服务不需要 API Key。

    ```bash
    export OPENAI_BASE_URL="https://your-endpoint.example/v1"
    export OPENAI_API_KEY="<endpoint-key>"
    export LOVIA_MODEL="<endpoint-model>"
    ```

=== "Anthropic-compatible"

    提供 Anthropic Messages 兼容协议的服务使用 `ANTHROPIC_BASE_URL`，模型名与官方 API
    一样带 `anthropic:` 前缀。

    ```bash
    export ANTHROPIC_BASE_URL="https://your-endpoint.example/anthropic"
    export ANTHROPIC_API_KEY="<endpoint-key>"
    export LOVIA_MODEL="anthropic:<endpoint-model>"
    ```

=== "Ollama"

    Ollama 的 OpenAI-compatible 端点无需密钥。请将模型名替换为本地已经拉取的模型。

    ```bash
    export OPENAI_BASE_URL="http://127.0.0.1:11434/v1"
    export LOVIA_MODEL="<ollama-model>"
    ```

    Ollama 会静默截断过长的提示词，因此应配置与 `num_ctx` 一致的
    `Compaction(context_window=...)`。详见 [Context Window](providers.md#上下文窗口)。

在 Agent 中直接传入端点使用的模型名。环境变量用于配置凭证和 Base URL，不会替 Python API
选择模型：

```python
from lovia import Agent

agent = Agent(name="assistant", model="<model>")
```

!!! note ".env 文件"

    Python 库不会自动加载 `.env`。你可以在 Shell 中导出变量、使用 `python-dotenv`，或直接
    在代码中传入配置。`lovia web` CLI 和仓库示例可以按各自文档加载相应的环境文件。

## 验证配置

```python
from lovia import Agent

agent = Agent(name="setup-check", model="<model>")
print(agent.run_sync("请只回复：lovia is ready").output)
```

配置完成后，继续阅读[快速上手](quickstart.md)。Provider 构造参数、端点方言判断、代理、TLS
和上下文窗口等内容，详见 [Provider 与模型](providers.md)。
