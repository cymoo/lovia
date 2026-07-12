# Installation

Install the small core first, then add integrations only when you use them.
lovia requires Python 3.10 or newer.

## Install the package

```bash
pip install lovia
```

| Capability | Install |
| --- | --- |
| Core agents, tools, providers, sessions, workspace | `pip install lovia` |
| MCP client support | `pip install "lovia[mcp]"` |
| DuckDuckGo search backend | `pip install "lovia[ddg]"` |
| FastAPI server, chat UI, and scheduling | `pip install "lovia[web]"` |

Extras compose normally, for example `pip install "lovia[mcp,web]"`.

## Configure a model

`Agent(model=...)` accepts a Provider instance or a model string. Bare model
names use the OpenAI-compatible adapter; `anthropic:` names use the
Anthropic-compatible adapter.

=== "OpenAI"

    The official endpoint is the default, so no base URL is required.

    ```bash
    export OPENAI_API_KEY="sk-..."
    export LOVIA_MODEL="<openai-model>"
    ```

=== "Anthropic"

    The `anthropic:` prefix selects the Anthropic Messages adapter.

    ```bash
    export ANTHROPIC_API_KEY="sk-ant-..."
    export LOVIA_MODEL="anthropic:<anthropic-model>"
    ```

=== "OpenAI-compatible"

    DeepSeek, vLLM, LM Studio, gateways, and similar services expose an
    OpenAI-compatible endpoint. Use the model name exactly as that service
    publishes it. Some local services do not require a key.

    ```bash
    export OPENAI_BASE_URL="https://your-endpoint.example/v1"
    export OPENAI_API_KEY="<endpoint-key>"
    export LOVIA_MODEL="<endpoint-model>"
    ```

=== "Anthropic-compatible"

    Services exposing the Anthropic Messages dialect use `ANTHROPIC_BASE_URL`
    and the same `anthropic:` model prefix as the official API.

    ```bash
    export ANTHROPIC_BASE_URL="https://your-endpoint.example/anthropic"
    export ANTHROPIC_API_KEY="<endpoint-key>"
    export LOVIA_MODEL="anthropic:<endpoint-model>"
    ```

=== "Ollama"

    Ollama's OpenAI-compatible endpoint is keyless. Replace the model with one
    you have pulled locally.

    ```bash
    export OPENAI_BASE_URL="http://127.0.0.1:11434/v1"
    export LOVIA_MODEL="<ollama-model>"
    ```

    Ollama silently truncates overlong prompts, so configure
    `Compaction(context_window=...)` to match its `num_ctx`; see
    [context windows](providers.md#context-windows).

Pass the endpoint's model name directly to the Agent. Environment variables
configure credentials and Base URLs; they do not choose a model for the Python
API:

```python
from lovia import Agent

agent = Agent(name="assistant", model="<model>")
```

!!! note ".env files"

    The Python library does not load `.env` implicitly. Export variables in
    your shell, load the file with `python-dotenv`, or pass configuration in
    code. The `lovia web` CLI and repository examples can load their documented
    env files for you.

## Verify the setup

```python
from lovia import Agent

agent = Agent(name="setup-check", model="<model>")
print(agent.run_sync("Reply with exactly: lovia is ready").output)
```

Then continue with the [Quickstart](quickstart.md). For provider constructor
options, endpoint dialect detection, proxies, TLS, and context windows, see
[Providers & models](providers.md).
