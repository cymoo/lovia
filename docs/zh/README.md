# lovia

**简洁、轻量、供应商中立的 Python Agent 框架。**先构建一个 Agent，再按需加入类型化工具、
持久化、插件、工作区或 Web UI，无需一开始就引入整套技术栈。

```bash
pip install lovia
```

<div class="grid cards" markdown>

-   **运行第一个 Agent**

    配置模型，几分钟内得到第一份可用结果。

    [开始快速上手 →](quickstart.md)

-   **理解运行机制**

    了解 Agent、Run、Turn、Tool、Transcript 和 Plugin 如何协同工作。

    [阅读核心概念 →](concepts.md)

-   **从示例开始构建**

    按功能浏览小而完整、可直接运行的脚本。

    [查看可运行示例 →](../../examples/README-zh.md)

-   **为生产环境做准备**

    补齐资源限制、持久化、安全门禁、可观测性和部署边界。

    [查看生产部署清单 →](deployment.md)

</div>

## 按目标选择指南

| 目标 | 指南 |
| --- | --- |
| 定义并运行 Agent | [Agent](agents.md) · [运行 Agent](running.md) |
| 配置模型或兼容端点 | [安装与模型配置](installation.md) · [Provider 与模型](providers.md) |
| 为模型提供能力 | [工具](tools.md) · [工作区](workspace.md) · [多 Agent](multi-agent.md) |
| 扩展 Agent | [插件](plugins.md) · [Skills](skills.md) · [MCP](mcp.md) · [记忆](memory.md) |
| 让运行安全、可靠、可恢复 | [可靠性](reliability.md) · [Session](sessions-and-checkpoints.md) · [人工审批](human-in-the-loop.md) |
| 提供服务并验证质量 | [Web 服务](web.md) · [HTTP API](http-api.md) · [测试](testing.md) · [评测](eval.md) |

如果要查找具体类型、异常或常见故障，请使用 [API 参考](api-reference.md)或
[故障排查](troubleshooting.md)。

!!! note "文档版本"

    本站内容跟随当前 `main` 分支。对照源码文档排查行为差异时，可运行
    `python -c "import lovia; print(lovia.__version__)"` 查看本地安装版本。

## 面向贡献者

[Architecture（英文）](../architecture.md)记录了模块结构、运行循环内部机制，以及修改
lovia 本身时需要遵守的不变量。

---

英文版：[docs/en](../en/README.md)。
