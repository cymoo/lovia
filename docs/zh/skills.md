# Skills

政策、运维手册（runbook）和风格指南不必全部塞进系统提示词。**Skill** 把这类知识组织成
可复用的指令包：模型先看到简短索引，需要时再加载正文和附属文件。

```python
from lovia import Agent, Skills

agent = Agent(
    name="support",
    instructions="按正确政策帮助顾客。",
    model="<model>",
    plugins=[Skills("./skills")],
)
```

## 渐进披露

Skill 占用的上下文分为三个阶段：

1. **索引**：始终放在系统提示词中。每个 Skill 一行
   （`` `name` — description``，加额外 frontmatter），后面跟使用规则。在真正需要前，
   一个 Skill 只占这点上下文。
2. **`load_skill(name)`**：插件提供的工具。当模型判断某个 Skill 适用时，返回完整
   `SKILL.md` 正文。
3. **`read_skill_file(name, relpath)`**：原样读取被引用的文件，比如
   `references/refund-tiers.md`、脚本或模板。

目录中的 Skill 会动态更新：每次列出索引都会重新扫描目录；每次加载正文都会实时读取磁盘。
如果要加载的名称不在上次扫描结果中，还会先重新扫描一次。因此新增、修改或删除 Skill 后，
无需重启进程即可生效。

## Skill 目录结构

一个 Skill 是一个目录，里面有带 YAML frontmatter 的 `SKILL.md`，以及可选支持文件：

```text
skills/
└── refund-policy/
    ├── SKILL.md
    ├── references/     # 按需加载的文档
    ├── scripts/        # Skill 可能提到的可执行脚本
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
  Skill**，而不只是说明它是什么。
- 其他 frontmatter key 会进入 `extra`，并展示在索引中；团队常用它放 tags、owner、version。

如果某个 Skill 的 `SKILL.md` 格式错误，扫描时会跳过并记录一次 warning 日志，不影响
`SkillCatalog` 加载其他 Skill。

## 配置

```python
Skills("./skills", "./team-skills")                # 多个目录，按顺序扫描
Skills("./skills", usage_rules="每次回复最多加载一个 Skill。")
Skills("./skills", filter=lambda meta: "internal" not in meta.extra.get("tags", []))
```

- **多个目录**会合并成一个 `SkillCatalog`；遇到同名 Skill 时，按扫描顺序保留第一个，其余项
  会记录日志并跳过。
- **`usage_rules`** 会替换索引后的默认使用规则；传 `""` 可以完全省略规则。
- **`filter`**（任意 `SkillFilter` 谓词）接收每个 Skill 的 `SkillMetadata`
  （`name`、`description`、`extra`），返回 `True` 表示保留。它会切实限制可见范围，而非仅作展示：
  被过滤掉的 Skill 不会出现在索引中，也无法通过工具加载。

Skill 相关错误会抛出 `SkillsError`，并附带 `skill_name`、`path` 和 `hint`。名称不存在时，
会抛出子类 `SkillNotFoundError`，其中还会列出当前可见的 Skill。模型通过工具加载内容时，
这些异常会被捕获，并转成普通错误文本返回。

## 自定义后端

目录只是一种来源；底层接口是公开的：

- **`SkillSource`**：Skill 存储接口（Protocol），包含两个异步方法：`async list_skills() ->
  list[SkillMetadata]` 和 `async load_skill(name) -> Skill`；名称不存在时，后者应抛出
  `SkillNotFoundError`。实现这个接口后，就可以从数据库、API 或对象存储提供 Skill。每次调用
  都应反映数据源的当前状态；接口刻意不提供 `reload()` / `refresh()`。如果实现内部使用缓存，
  应在这两个方法中自行处理更新。内置的 `DirectorySkillSource(*roots)` 会在每次列出索引时
  扫描目录；加载时若找不到名称，也会重扫一次再判定不存在。
- **`SkillCatalog`**：把数据源、使用规则和筛选条件封装在一起，提供插件所需的
  `instructions()` / `tools()`，以及供代码直接调用的 `list_skills()` / `load_skill()`。
  如果需要在代码中访问 Skill，或让多个地方共用同一套配置，可以直接创建 `SkillCatalog`
  （使用 `SkillCatalog.from_dir(...)`，或传入自定义的 `SkillSource`）：

```python
from lovia.plugins import SkillCatalog, Skills

catalog = SkillCatalog(MyDbSkillSource(), usage_rules="…")
agent = Agent(..., plugins=[Skills(catalog)])
```

（向 `Skills` 传入 `SkillCatalog` 时，不能再同时指定 `usage_rules=` 或 `filter=`；
请在创建 `SkillCatalog` 时完成这些配置。）

## 安全措施

- **阻止路径穿越**：`read_skill_file` 会解析目标路径，并要求它仍在 Skill 目录内；
  Skill 名也会直接拒绝 `/`、`\` 和 `..`。
- **将加载内容明确标为资料**：两个工具都会把返回内容放在 BEGIN/END 标记之间，并对内容中伪造的
  同类标记进行转义。工具描述要求模型把标记内的内容当作参考资料。这段约束不必在每次返回时重复，
  也便于提示词缓存复用。因此，Skill 文件中指令的优先级低于系统提示词、用户请求和安全规则。
  每次输出最多 10 万个字符。
- **frontmatter 不能改变索引结构**：写入系统提示词前，`description` 和其他 frontmatter 字段
  会被压成单行，额外字段的长度也有限制，无法借此注入新的提示词结构。

## 使用建议

- **Skill 文件 I/O 不受工作区 ACL 限制。** `load_skill` 和 `read_skill_file` 会自行读取文件；
  即使 Skill 目录在[工作区](workspace.md)根目录之外，或匹配 `denied_paths`，也仍能加载。
  请把 Skill 目录视为可信内容；只有**执行**附带脚本时才会经过工作区 Shell 策略。
- **`description` 决定模型何时加载 Skill。** 写得太模糊，模型可能始终不加载，或每次都加载。
  请像编写工具描述一样，说明适用任务、具体场景和触发词。
- **提示词索引在每次运行开始时更新。** 运行期间新增的 Skill 不会自动加入当前索引；但只要
  Agent 已经知道名称，例如这个 Skill 刚由 Agent 自己创建，就可以立即通过 `load_skill`
  加载。到下一次运行时，它才会出现在系统提示词的索引中。

## 延伸阅读

- [插件](plugins.md)：Skills 底层使用的机制
- [记忆](memory.md)：agent 自己积累的知识，而不是你预先写好的知识
- 示例：[`22_skills.py`](../../examples/22_skills.py)，示例 Skill：
  [`examples/skills/refund-policy/`](../../examples/skills/refund-policy/)
