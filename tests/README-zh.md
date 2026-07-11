# lovia 测试套件

[English](./README.md)

本测试套件基于标准的 `pytest`，根目录即当前目录（[`pytest.ini`](../pytest.ini)
中配置了 `testpaths = tests`）。默认测试均可确定性复现，且完全不访问网络；另有少量
需要显式启用的 **live** 测试，用于调用真实的模型服务端点，并通过 marker 与默认测试
隔离。

## 目录结构

```
tests/
├── conftest.py            共享 fixture
├── scripted_provider.py   重新导出 lovia.testing.ScriptedProvider（兼容层）
├── test_*.py              顶层测试套件（runner、transcript、hooks、schema 等）
├── context/               上下文压缩：流水线、阶段、token、渲染、状态等
│   ├── test_live_context.py         live：摘要、清除、卸载、召回、溢出
│   ├── test_ratio_convergence.py    live：校准比例的收敛性
│   └── ratio_calibration/           研究脚本和报告生成器（并非测试，详见其 README）
├── providers/             openai_chat、anthropic 适配器（含 test_live.py）
├── runtime/               运行循环、检查点、引导、预算
├── plugins/               mcp、skills、todo、memory（memory 含 live 测试）
├── eval/                  lovia.eval 框架（含 test_live.py）
├── stores/ · tools/ · web/ · workspace/
```

辅助代码（`conftest.py`、`scripted_provider.py`）与测试放在同一目录，但它们**不会被
作为测试收集**，因为文件发现规则仅匹配 `test_*.py`。

## 运行测试

| 任务 | 命令 |
| --- | --- |
| 运行全部测试（快速、离线） | `pytest` |
| 运行单个测试 | `pytest tests/test_runner.py::test_plain_text_run` |
| 运行单个目录 | `pytest tests/context` |
| 查看覆盖率 | `pytest --cov=lovia --cov-report=term-missing` |
| 检查代码 / 格式化 | `ruff check .` · `ruff format .` |

说明：

- **`asyncio_mode = auto`**：`async def test_…` 无需添加
  `@pytest.mark.asyncio` 装饰器即可运行。
- 默认测试使用 **`lovia.testing.ScriptedProvider`** 驱动模型，结果可确定性复现，
  且不访问网络。编写测试时应优先使用它，避免调用真实端点。
- 如果希望自动使用项目虚拟环境，可运行 `uv run pytest …`。

## Live（模型服务）测试

这类测试需要**显式启用**，并会调用 `.env` 中配置的端点。本仓库已适配 DeepSeek
提供的 OpenAI 和 Anthropic 兼容 API。每个 live 测试文件都会自行加载 `.env`，
因此无需预先 `export` 任何变量。

### 通过 marker 筛选测试

marker 有两种使用方式。如果逐个搜索测试函数，却找不到
`@pytest.mark.live_provider`，这是正常现象：大多数 live 测试文件只在**模块级别**
设置一次 marker，该标记会应用于文件中的所有测试：

```python
# tests/context/test_live_context.py、providers/test_live.py、eval/test_live.py、
# context/test_ratio_convergence.py
pytestmark = pytest.mark.live_provider          # 标记整个模块
```

`plugins/` 下的两个文件同时包含 live 测试和离线测试，因此使用逐函数装饰器
（`@pytest.mark.live_provider`）。两种写法均可被 pytest 正确识别。可通过以下命令
检查测试划分：

```bash
pytest -m live_provider     --collect-only -q | grep -c ::   # live 测试（约 30 个）
pytest -m "not live_provider" --collect-only -q | grep -c ::   # 其余测试
```

### 运行标准 live 测试

```bash
LOVIA_LIVE_TESTS=1 pytest -m live_provider
```

该命令会运行六个文件中的标准 live 测试，涵盖 context、providers、eval、memory
和 ratio-convergence。

### 启用可选测试

`LOVIA_LIVE_TESTS=1` 会启用大部分 live 测试。少数测试设有**双重开关**，必须额外
设置相应的环境变量，否则仍会被跳过：

| 环境变量 | 启用的测试 | 备注 |
| --- | --- | --- |
| `LOVIA_LIVE_TESTS=1` | 全部标准 live 测试 | 必须设置，同时还需提供相应的 API 密钥 |
| `LOVIA_LIVE_OVERFLOW_TESTS=1` | 真实溢出探测 | 会刻意发送超长提示词 |
| `LOVIA_LIVE_OPENAI_CONTENT_TESTS=1` | OpenAI 图片和文件内容块 | 否则仅在官方端点上运行 |
| `LOVIA_LIVE_ANTHROPIC_CONTENT_TESTS=1` | Anthropic 图片、PDF 和文件块 | 否则仅在官方端点上运行 |
| `LOVIA_ANTHROPIC_PDF_OK` / `LOVIA_ANTHROPIC_TEXT_FILE_OK` / `LOVIA_OPENAI_FILE_OK` | 特定的文件能力断言 | 端点必须确实支持相应模态 |

运行所有 live 测试：

```bash
LOVIA_LIVE_TESTS=1 \
LOVIA_LIVE_OVERFLOW_TESTS=1 \
LOVIA_LIVE_OPENAI_CONTENT_TESTS=1 \
LOVIA_LIVE_ANTHROPIC_CONTENT_TESTS=1 \
pytest -m live_provider
```

> ⚠️ 启用内容测试的环境变量后，图片、PDF 和文件测试将实际运行。如果当前端点
>（例如 DeepSeek）不支持相应模态，测试会直接**失败**，而不是被跳过。使用非官方
>端点时，通常应运行前面那条未启用额外内容测试的命令。

### 校准研究

`context/ratio_calibration/` 是一套独立的研究脚本和报告生成器，不属于 pytest
收集的测试。它会调用真实端点，并生成
[`docs/ratio-calibration.md`](../docs/ratio-calibration.md)。pytest 实际收集的入口是
`context/test_ratio_convergence.py`，它会通过一次小规模运行验证相关不变量。完整研究
的运行方式请参阅该目录下的 `README.md`。
