# 技能

政策、运维手册（runbook）、风格指南等团队知识，不应全部塞进每次请求都要付费传输的系统提示词。
**技能（skill）**是一种可复用的指令包：模型可以用很低的成本发现它，并且只在需要时加载。
lovia 遵循 Agent Skills 约定，以 `SKILL.md` 搭配附属文件组织技能。

```python
from lovia import Agent, Skills

agent = Agent(
    name="support",
    instructions="按正确政策帮助顾客。",
    model="glm-5.2",
    plugins=[Skills("./skills")],
)
```

## 渐进披露

技能占用的上下文分为三个阶段：

1. **索引**：始终放在 system prompt 中。每个 skill 一行
   （`` `name` — description``，加额外 frontmatter），后面跟使用规则。在真正需要前，
   一个 skill 只占这点上下文。
2. **`load_skill(name)`**：插件提供的工具。当模型判断某个 skill 适用时，返回完整
   `SKILL.md` 正文。
3. **`read_skill_file(name, relpath)`**：原样读取被引用的文件，比如
   `references/refund-tiers.md`、脚本或模板。

正文每次加载都会从磁盘按需读取，不缓存。因此修改 skill 后，下一次调用就会生效，不必重启。

## 技能的目录结构

一个 skill 是一个目录，里面有带 YAML frontmatter 的 `SKILL.md`，以及可选支持文件：

```text
skills/
└── refund-policy/
    ├── SKILL.md
    ├── references/     # 按需加载的文档
    ├── scripts/        # skill 可能提到的可执行脚本
    └── assets/         # 模板、fixture
```

```markdown
---
name: refund-policy
description: 如何按用户等级评估并处理退款请求。
---

# 退款政策

当客户要求退款时，先判断用户等级……
等级表见 [references/refund-tiers.md](references/refund-tiers.md)。
```

- `name`：由 `[a-zA-Z0-9]` 片段通过 `-` / `_` 连接，最长 64 字符；省略时用目录名。
- `description`：必填，最长 1024 字符。它是模型的路由信号，所以要写清**什么时候用这个
  skill**，而不只是说明它是什么。
- 其他 frontmatter key 会进入 `extra`，并展示在索引中；团队常用它放 tags、owner、version。

如果某个 skill 的 `SKILL.md` 格式错误，扫描时会跳过并记录 warning 日志；catalog 里其他 skill
仍会加载。

## 配置

```python
Skills("./skills", "./team-skills")                # 多个目录，按顺序扫描
Skills("./skills", usage_rules="每次回复最多加载一个 skill。")
Skills("./skills", filter=lambda meta: "internal" not in meta.extra.get("tags", []))
```

- **多个目录**会合并成一个 catalog；skill 名重复时，第一次出现的胜出，后面的会记录日志并跳过。
- **`usage_rules`** 会替换索引后的默认使用规则；传 `""` 可以完全省略规则。
- **`filter`**（任意 `SkillFilter` 谓词）接收每个 skill 的 `SkillMetadata`
  （`name`、`description`、`extra`），返回 `True` 表示保留。它会切实限制可见范围，而非仅作展示：
  被过滤掉的 skill 不会出现在索引中，也无法通过工具加载。

skill 层失败会在 setup 时抛 `SkillsError`（带 `skill_name`/`path`/`hint`）；工具内部的失败会
被捕获，并以普通错误字符串返回给模型。

## 自定义后端

目录只是一种来源；底层接口是公开的：

- **`SkillSource`**：存储 protocol。`metadata` 属性列出 `SkillMetadata`，
  `async load(name) -> Skill` 加载 skill。你可以实现它，从数据库、API 或对象存储提供 skill。
  内置 `LocalDirSkillSource(*roots)`，并为长生命周期进程提供 `rescan()`。
- **`SkillCategory`**：一个 source 加上 rules/filter，并提供插件需要的 `instructions()` 和
  `tools()`。当你需要程序化访问或共享一份配置好的 catalog 时，可以直接构建它
  （`SkillCategory.from_dir(...)`，或包住自己的 source）：

```python
from lovia.plugins import SkillCategory, Skills

catalog = SkillCategory(MyDbSkillSource(), usage_rules="…")
agent = Agent(..., plugins=[Skills(catalog)])
```

（把 `SkillCategory` 传给 `Skills` 的同时再传 `usage_rules=` / `filter=` 会被拒绝；
请在 category 上配置。）

## 安全措施

- **阻止路径穿越**：`read_skill_file` 会解析目标路径，并要求它仍在 skill 目录内；
  skill 名也会直接拒绝 `/`、`\` 和 `..`。
- **加载内容会被框定为数据**：`load_skill` 会用 BEGIN/END reference-material marker 包住正文
  （并中和正文内伪造的 marker），所以 skill 文件里的 instructions 弱于你的 system prompt；
  输出会在 100k 字符处截断。

## 注意事项

- **skill 文件 IO 绕过工作区 ACL。** `load_skill` 和 `read_skill_file` 做自己的读取；
  即使 skill 目录在[工作区](workspace.md)根目录之外，或匹配 `denied_paths`，也仍能加载。
  请把 skill 目录视为可信内容；只有**执行**附带脚本时才会经过工作区 shell 策略。
- **description 是路由信号。** 模糊 description 会让模型永远不加载（或总是加载）这个 skill。
  像写工具 description 一样写它：任务化、具体、带触发词。
- **索引在一次运行中静态。** 目录中新加的 skill 会在下一次运行出现
  （或长生命周期 source 调用 `rescan()` 后出现），不会在当前对话中途出现。

## 延伸阅读

- [插件](plugins.md)：skills 底层使用的机制
- [记忆](memory.md)：agent 自己积累的知识，而不是你预先写好的知识
- 示例：[`22_skills.py`](../../examples/22_skills.py)，示例 skill：
  [`examples/skills/refund-policy/`](../../examples/skills/refund-policy/)
