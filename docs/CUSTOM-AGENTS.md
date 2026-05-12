# 自定义 Agent（启动会话时指定）

通过一个 YAML 配置文件，可以在新建会话时挂载一个**自定义 Agent**，精确控制它能使用的工具、技能（skills）、MCP 服务器、系统提示词，以及是否注入项目知识库。常见用途：

- 只读代码审查员（禁用所有写文件 / Shell 工具）
- 沙盒研究员（限制只能用网络搜索 + 阅读工具）
- 角色化助手（自定义系统提示词、给定一个独立的角色身份）
- 收紧权限的演示账号（只放开你信任的 MCP 工具）

---

## 1. 配置文件位置

自定义 Agent 由 YAML 文件描述，按以下优先级被发现：

| 路径 | 范围 | 说明 |
|---|---|---|
| `<work_dir>/.kimi/agents/*.yaml` | **项目级**（project） | 仅在工作目录是 `<work_dir>` 时可见。同名时**覆盖**用户级。 |
| `~/.kimi/agents/*.yaml` | **用户级**（user） | 对所有会话可见。 |

> 文件名（去掉 `.yaml`）即默认 Agent 名；可以用 YAML 内的 `agent.name` 字段覆盖。

---

## 2. 启动会话时使用

### Web UI

打开"新建会话"对话框 → 展开底部的「**高级设置**」面板 → 在 **Agent** 下拉框中选择你要挂载的 Agent。

- 对话框打开时，下拉框先加载 `~/.kimi/agents/` 中的用户级 Agent。
- 当你高亮某个工作目录后（键盘上下移动也算），系统会自动追加该工作目录下 `<work_dir>/.kimi/agents/` 的项目级 Agent；同名时项目级覆盖用户级。
- 默认值是 "Default agent"（即内置 `default` Agent，等同于旧行为）。

### CLI

```bash
# 用一个已发现的 Agent 名字（按 work_dir 自动解析）
kimi --agent code-reviewer

# 直接指定 YAML 文件
kimi --agent-file /path/to/my-agent.yaml
```

`--agent <name>` 的解析顺序：

1. 先在 `<work_dir>/.kimi/agents/*.yaml` 查找；
2. 再在 `~/.kimi/agents/*.yaml` 查找；
3. 最后回退到内置 Agent 名（如 `default`、`okabe`）。

### 会话恢复

被选中的 Agent 会以**绝对路径**形式持久化到 `<session_dir>/session_config.json`，键名 `agent_spec_path`。会话恢复时 worker 会重新读取并使用同一个 spec。**如果这个文件在两次会话之间被删了，worker 会硬失败**——这是为了避免"静默退回默认 Agent"造成的权限漂移。

---

## 3. 完整字段参考

以下字段都放在 YAML 的 `agent:` 节点下。除注明外，全部可选。

### 3.1 基础

| 字段 | 类型 | 说明 |
|---|---|---|
| `extend` | string | 继承另一个 spec。值是 `default`（内置默认 Agent），或一个相对当前文件的路径。 |
| `name` | string | Agent 名。Web 下拉框和 CLI `--agent` 都以此为准。 |
| `when_to_use` | string | 简短描述，告诉用户/上层 Agent 什么时候挂载这个 Agent。 |
| `model` | string | 覆盖默认 LLM 模型 alias。 |

### 3.2 系统提示词

| 字段 | 类型 | 说明 |
|---|---|---|
| `system_prompt_path` | path | 一个 `.md` 模板（Jinja2），相对当前 YAML 文件。 |
| `system_prompt_args` | map | 注入模板的变量；最常用的是 `ROLE_ADDITIONAL`，会附加到默认模板的角色段。 |

> 推荐做法：`extend: default` 后只覆盖 `system_prompt_args.ROLE_ADDITIONAL`，省得维护一整套模板。

### 3.3 工具（内置）

| 字段 | 类型 | 语义 |
|---|---|---|
| `tools` | list[str] \| `null` | 完整工具清单。一般通过 `extend` 继承，自己不写。 |
| `allowed_tools` | list[str] \| `null` | 白名单子集。`null` = 不过滤；`[]` = 严格空（零个工具）。 |
| `exclude_tools` | list[str] | 黑名单。**优先级高于** `allowed_tools`。 |

工具名是 Python 导入路径，形如 `"kimi_cli.tools.file:WriteFile"`。常见可写工具：
`kimi_cli.tools.file:WriteFile`、`kimi_cli.tools.file:StrReplaceFile`、`kimi_cli.tools.shell:Shell`、`kimi_cli.tools.memory:Memory`。

### 3.4 Skills（新）

| 字段 | 类型 | 语义 |
|---|---|---|
| `allowed_skills` | list[str] \| `null` | 注入到系统提示词中的 skill 列表白名单。`null` = 不过滤；`[]` = 严格空（不暴露任何 skill）。 |
| `excluded_skills` | list[str] | 黑名单。优先级高于白名单。 |

匹配规则：**大小写不敏感**。Skill 名以发现时的 `SKILL.md` frontmatter `name` 字段为准。

### 3.5 MCP 权限（新）

| 字段 | 类型 | 语义 |
|---|---|---|
| `allowed_mcp_servers` | list[str] \| `null` | 只暴露这些服务器的工具。`null` = 不过滤。 |
| `allowed_mcp_tools` | list[str] \| `null` | 更细粒度白名单，格式 `"server:tool"`。 |
| `excluded_mcp_tools` | list[str] | 黑名单，格式 `"server:tool"`；优先级高于白名单。 |

> 即时加载和后台延迟加载（`start_mcp_loading=True`）两条路径都会被过滤，不会绕开。

### 3.6 知识库（新）

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `knowledge_base` | bool | `true` | 是否把 `<work_dir>/.kimi/memory/knowledge/index.md` 注入到系统提示词。 |

设为 `false` 时，`KIMI_KNOWLEDGE_BASE` 模板变量被替换为空字符串；wiki 详情文件仍可被 `ReadFile` 工具按需访问（除非你也把它从 `tools` 里拿掉）。

### 3.7 子 Agent

| 字段 | 类型 | 说明 |
|---|---|---|
| `subagents` | map \| `~` | `{name: {path, description}}` 形式。子 Agent **不会**自动继承父 spec 的新字段——除非它们自己也 `extend:` 父文件。 |

---

## 4. 完整示例

### 4.1 只读代码审查员（推荐起点）

`~/.kimi/agents/code-reviewer.yaml`：

```yaml
version: 1
agent:
  extend: default
  name: code-reviewer
  when_to_use: "Read-only code reviewer with security skills"
  system_prompt_args:
    ROLE_ADDITIONAL: |
      You are a code reviewer. Describe findings; never propose patches.
      You do not write or edit files.
  exclude_tools:
    - kimi_cli.tools.file:WriteFile
    - kimi_cli.tools.file:StrReplaceFile
    - kimi_cli.tools.memory:Memory
  allowed_skills: [security-review]
  allowed_mcp_servers: [github]
  excluded_mcp_tools: [github:create_pr, github:merge_pr]
  knowledge_base: true
  subagents: ~
```

### 4.2 最小化沙盒测试 Agent

`<work_dir>/.kimi/agents/test-agent.yaml`：

```yaml
version: 1
agent:
  extend: default
  name: test-agent
  when_to_use: "Sandboxed read-only test agent"
  system_prompt_args:
    ROLE_ADDITIONAL: |
      You are TestAgent. Briefly introduce yourself and list restrictions
      on first turn.
  exclude_tools:
    - kimi_cli.tools.file:WriteFile
    - kimi_cli.tools.file:StrReplaceFile
    - kimi_cli.tools.shell:Shell
    - kimi_cli.tools.memory:Memory
  allowed_skills: []      # 严格空：不暴露任何 skill
  knowledge_base: false   # 不注入项目知识库
  subagents: ~
```

### 4.3 完全自定义（不继承默认）

`~/.kimi/agents/web-researcher.yaml`：

```yaml
version: 1
agent:
  name: web-researcher
  system_prompt_path: ./web-researcher.md   # 自己写一份模板
  tools:
    - kimi_cli.tools.ask_user:AskUserQuestion
    - kimi_cli.tools.todo:SetTodoList
    - kimi_cli.tools.file:ReadFile
    - kimi_cli.tools.file:Glob
    - kimi_cli.tools.file:Grep
    - kimi_cli.tools.web:SearchWeb
    - kimi_cli.tools.web:FetchURL
  knowledge_base: false
  subagents: ~
```

---

## 5. 字段语义速查（坑点）

| 表达式 | 含义 |
|---|---|
| `allowed_xxx: null` 或不写 | **不过滤** — 全部允许通过 |
| `allowed_xxx: []` | **严格空** — 一个都不允许 |
| `excluded_xxx: [...]` | 总是优先于 `allowed_xxx` |
| `extend: default` | 内置默认 Agent（推荐起点） |
| `extend: ./xxx.yaml` | 同目录的另一个 spec 文件 |

---

## 6. 工作原理（简）

1. **发现**：`discover_user_agent_specs(work_dir)` 遍历 `<work_dir>/.kimi/agents/*.yaml` → `~/.kimi/agents/*.yaml`，项目级覆盖用户级。
2. **解析**：`load_agent_spec(path)` 递归处理 `extend:`，把每个 `Inherit` 哨兵字段从父 spec 合并下来，最后落到一个不可变的 `ResolvedAgentSpec`。
3. **绑定**：会话启动时
   - 内置工具走现有的 `tools`/`allowed_tools`/`exclude_tools` 流程；
   - Skills 在被格式化进系统提示词**之前**经 `filter_skills` 过滤；
   - 知识库根据 `knowledge_base` 决定是否注入；
   - MCP 工具加载完成后立刻经 `KimiToolset.filter_mcp_tools()` 过滤（即时和延迟路径都会触发）。
4. **持久化**：选中的 spec 绝对路径写入 `<session_dir>/session_config.json` 的 `agent_spec_path`，会话恢复时直接读取。

---

## 7. 注意事项

- **不支持热重载**。改完 YAML 需要新建会话才生效。
- **子 Agent 不自动继承新字段**。如果希望子 Agent 同样被收紧权限，让它 `extend:` 父文件。
- **`AskUserQuestion` 不要在 Web 模式下排除**。Web 模式没有命令行交互，禁用它会让会话卡死。
- **`read_only` 不是一个字段**。要做只读模式，自己在 `exclude_tools` 里列写文件 / Shell / Memory；这样比"魔法开关"更显式、更可审计。
- **MCP 工具命名以 `server:tool` 形式过滤**。`server` 是 MCP 服务器的注册名；`tool` 是 MCP 协议返回的工具名（无前缀）。
- **空 spec 文件 / 解析失败**：Web 启动会返回 HTTP 400；CLI 会抛 `typer.BadParameter` 并列出当前可用名。

---

## 8. 相关源代码

| 主题 | 文件 |
|---|---|
| Spec 解析、继承、发现 | `kimi-cli/src/kimi_cli/agentspec.py` |
| Skill 过滤 | `kimi-cli/src/kimi_cli/skill/__init__.py` |
| MCP 过滤 | `kimi-cli/src/kimi_cli/soul/toolset.py` |
| 绑定到 Soul、知识库开关 | `kimi-cli/src/kimi_cli/soul/agent.py` |
| CLI 参数解析 | `kimi-cli/src/kimi_cli/cli/__init__.py` |
| Web 会话创建、发现 API | `kimi-cli/src/kimi_cli/web/api/sessions.py` |
| Web Worker 加载 spec | `kimi-cli/src/kimi_cli/web/runner/worker.py` |
| 前端下拉框 | `kimi-cli/web/src/features/sessions/create-session-dialog.tsx` |
