# 新建 Session 时 Agent System Prompt 加载逻辑

本文档记录在 Web UI / CLI 新建 session 时，`kimi-cli` 后端如何选择 agent、组装并注入 system prompt 的完整链路。所有路径均相对仓库根 `/Users/jie/Develop/projects/kimi_agent`。

## 1. Web 入口

`kimi-cli/src/kimi_cli/web/api/sessions.py:324-390` `create_session()`：

- 接收请求字段 `work_dir` 与可选 `agent_name`
- 若指定 `agent_name`，调用 `discover_user_agent_specs(work_dir)`（`kimi-cli/src/kimi_cli/agentspec.py:227-245`）将名称解析为实际 spec 路径
  - 扫描优先级：项目 `{work_dir}/.kimi/agents/*.yaml` > 用户 `~/.kimi/agents/*.yaml`（项目级覆盖用户级）
  - 自定义 Agent（v0.1.4 引入，commit `375be99`）写盘 / 读取都走这两个目录
- 创建会话：`await KimiCLISession.create(work_dir=work_dir)`（`kimi-cli/src/kimi_cli/session.py:130-181`），生成 UUID 与 `{work_dir}/.kimi/sessions/{session_id}/state.json`

## 2. Agent Spec 加载

`kimi-cli/src/kimi_cli/agentspec.py:110-224` `load_agent_spec()` → `_load_agent_spec()`（递归）：

- 支持 `extend: "default"` 或相对路径继承，按链 merge 配置
- 默认 fallback agent：`kimi-cli/src/kimi_cli/agents/default/agent.yaml`（常量定义见 `agentspec.py:20`）
- 系统提示路径相对于 `agent.yaml` 所在目录解析为绝对路径（`agentspec.py:179-182`）
- 返回 `ResolvedAgentSpec`，含 `system_prompt_path`、`system_prompt_args`、`allowed_skills` / `excluded_skills`、`knowledge_base` 等字段

## 3. YAML Agent Spec vs AGENTS.md

两者是**完全独立的两套机制**，职责不重叠。

| | YAML agent spec | AGENTS.md |
|---|---|---|
| **回答的问题** | "我要启用哪一个 Agent？" | "Agent 在这个项目里要遵循什么规则/上下文？" |
| **位置** | `~/.kimi/agents/*.yaml` 或 `{work_dir}/.kimi/agents/*.yaml` | `{work_dir}` 及其上层目录中的 `.kimi/AGENTS.md` / `AGENTS.md` / `agents.md` |
| **格式** | 结构化 YAML（pydantic schema） | 自由 Markdown |
| **加载入口** | `discover_user_agent_specs()` → `load_agent_spec()` | `load_agents_md(work_dir)` |
| **生命周期** | 新建 session 时**选中一份**作为 agent 定义本体 | 加载所有命中的，与 agent 选择无关 |
| **在 system prompt 中的位置** | 整体是模板来源（`system_prompt_path` 指向的 `.md` 经 Jinja2 渲染） | 被填入模板变量 `${KIMI_AGENTS_MD}`；agent 模板必须主动引用才生效 |
| **继承 / 复用** | 支持 `extend: default` 链式继承 | 不支持继承，按目录层级 root→leaf 拼接 |

### 3.1 YAML agent spec — Agent 的"身份证"

`AgentSpec` schema（`kimi-cli/src/kimi_cli/agentspec.py:31-70`）字段：

- `name` / `when_to_use` — 名称与触发提示
- `system_prompt_path` — **指向**一个 `.md` 模板文件（不是 `AGENTS.md`），相对 yaml 所在目录解析
- `system_prompt_args` — 传给 Jinja2 模板的自定义变量
- `model` — 默认模型 alias
- `tools` / `allowed_tools` / `exclude_tools` — 内置工具白/黑名单
- `subagents` — 子 agent 路由表（如 default 里挂的 `coder` / `explore` / `plan`）
- `allowed_skills` / `excluded_skills` — Skills 过滤
- `allowed_mcp_servers` / `allowed_mcp_tools` / `excluded_mcp_tools` — MCP 控制
- `knowledge_base: false` — 关闭项目知识库注入
- `extend: default` — 继承另一份 spec，未指定字段从父继承

示例：`kimi-cli/src/kimi_cli/agents/default/agent.yaml` 在一份文件里声明 `tools`、`subagents` 和 `system_prompt_path: ./system.md`。

### 3.2 AGENTS.md — 项目级"附加指令"

由 `load_agents_md()`（`kimi-cli/src/kimi_cli/soul/agent.py:94-175`）沿 `project_root → work_dir` 扫描，拼成单串塞进 `KIMI_AGENTS_MD`。它**不定义工具、不定义模型、不影响 agent 路由**，作用近似 `CLAUDE.md`。

**同一目录内的共存规则**（`agent.py:117-129`）：

| 候选 | 关系 |
|---|---|
| `.kimi/AGENTS.md` ↔ `AGENTS.md` | **共存**，都加载，`.kimi/` 在前 |
| `.kimi/AGENTS.md` ↔ `agents.md` | **共存**，都加载，`.kimi/` 在前 |
| `AGENTS.md` ↔ `agents.md` | **互斥**，大写优先（命中后 `break`） |

**跨目录拼接**：`_dirs_root_to_leaf(work_dir, project_root)`（`agent.py:78-91`）返回 root → leaf 目录列表，每层按上面规则收集；最终按 root → leaf 拼接，每段前带 `<!-- From: <path> -->` 注释。越靠近 `work_dir`（越具体）的内容排在越后面，且在 32 KiB 预算分配时享有**叶优先**保护（`agent.py:151-167`），不会被浅层目录的内容挤掉。

### 3.3 易混淆位置

- **`~/.kimi/agents/AGENTS.md` 不会被加载**：`load_agents_md()` 只扫 `work_dir` 路径树，**不会**进入 `~/.kimi/agents/`；同时 `discover_user_agent_specs()` 只 glob `*.yaml`（`agentspec.py:240`），也不会把 `AGENTS.md` 误读成 agent spec。这份文件目前没有任何加载点。
- **没有"全局 AGENTS.md"机制**：若想把通用指令注入到所有 session 的 system prompt，需要扩展 `load_agents_md` 增加用户级（`~/.kimi/AGENTS.md` 之类）扫描点，框架默认不支持。

## 4. System Prompt 三阶段组装

### 阶段 A — Builtin Args 构造

`kimi-cli/src/kimi_cli/soul/agent.py:221-358` `Runtime.create()` 并发收集上下文，构造 `BuiltinSystemPromptArgs`：

| 模板变量 | 来源 | 备注 |
|---|---|---|
| `KIMI_AGENTS_MD` | `load_agents_md(work_dir)`（`agent.py:94-175`） | 见 §3.2 的发现与拼接规则 |
| `KIMI_SKILLS` | `resolve_skills_roots()` → `discover_skills_from_roots()` → `index_skills()` → `format_skills_for_prompt()`（`agent.py:246-258`） | Skills 索引化为提示文本 |
| `KIMI_KNOWLEDGE_BASE` | `load_knowledge_base(work_dir)` | |
| `KIMI_WORK_DIR_LS` | `list_directory(work_dir)` | |
| `KIMI_WORK_DIR` | 当前工作目录 | |
| `KIMI_OS` / `KIMI_SHELL` | `Environment.detect()` | |
| `KIMI_NOW` | ISO 时间戳 | |
| `KIMI_OUTPUT_DIR` | 硬编码 `/app/output`（`agent.py:334`） | 不跟随 `work_dir` |
| `KIMI_ADDITIONAL_DIRS_INFO` | 附加目录信息 | |

### 阶段 B — Agent Spec → System Prompt 模板渲染

`kimi-cli/src/kimi_cli/soul/agent.py:405-525` `load_agent()`：

1. `_apply_spec_to_builtin_args()`（`agent.py:528-551`）：根据 spec 的 `allowed_skills` / `excluded_skills` 过滤 `KIMI_SKILLS`；若 `knowledge_base: false` 则清空 `KIMI_KNOWLEDGE_BASE`
2. `_load_system_prompt()`（`agent.py:554-579`）：Jinja2 渲染（变量分隔符 `${...}`），将上一步得到的 builtin args 合并 spec 的 `system_prompt_args` 后填入模板
3. 结果由 `KimiSoul` 持久化：`await self._context.write_system_prompt(self._agent.system_prompt)`（`kimi-cli/src/kimi_cli/soul/kimisoul.py:1217`）

### 阶段 C — 运行时动态注入

`kimi-cli/src/kimi_cli/soul/kimisoul.py:172-231` `KimiSoul.__init__()` 注册 4 个 `DynamicInjectionProvider`（`kimisoul.py:211-222`）：

- `PlanModeInjectionProvider`
- `AfkModeInjectionProvider`（可选）
- `SessionMemoryInjectionProvider`
- `CrossSessionMemoryInjectionProvider`（**仅 root agent**）

这些 provider **不修改静态 `system_prompt`**，而是在每个 turn 由 `_collect_injections()`（`kimisoul.py:291-304`）按需拼到消息流。

## 5. 新建 vs 恢复 vs Subagent

| 路径 | 行为 |
|---|---|
| 新建 session | 走完整阶段 A → B → C |
| 恢复 session | 使用相同加载逻辑；system prompt **用当前 spec 重新组装**，不是保存时的快照（`session_state.py:105-119`） |
| Subagent | `Runtime.copy_for_subagent()`（`agent.py:360-391`）共享父 `builtin_args`、独立 `DenwaRenji`，再走相同的 `load_agent()`（`kimi-cli/src/kimi_cli/subagents/builder.py:12-36`） |

## 6. 已知行为与注意点

- **AGENTS.md 截断策略**：总大小超 32 KiB 时浅层目录内容被丢弃以保留深层（项目级）内容（`agent.py:151-167`，常量 `_AGENTS_MD_MAX_BYTES`）。
- **Skills 双重过滤**：阶段 B 渲染时按 spec 过滤一次，toolset 加载（`agent.py:470-474`）再过滤一次。
- **`KIMI_OUTPUT_DIR` 硬编码** `/app/output`，容器外运行时可能不正确。
- **Subagent 拿不到 cross-session memory**：subagent 的 injection provider 列表不包含 `CrossSessionMemoryInjectionProvider`（`kimisoul.py:220-221`）。
- **恢复 session 不锁定 spec 版本**：用户修改 agent yaml 后恢复旧 session，新的 system prompt 会被使用。

## 7. 调用链速查

```
POST /api/sessions
  └─ create_session()                          [web/api/sessions.py:324]
     ├─ discover_user_agent_specs(work_dir)    [agentspec.py:227]
     │   scan {work_dir}/.kimi/agents/ + ~/.kimi/agents/
     ├─ KimiCLISession.create()                [session.py:130]
     │   mkdir {work_dir}/.kimi/sessions/{id}/state.json
     └─ backend worker
        └─ Runtime.create()                    [soul/agent.py:221]
           ├─ load_agents_md(work_dir)         [soul/agent.py:94]
           ├─ discover_skills_from_roots()
           ├─ load_knowledge_base(work_dir)
           ├─ Environment.detect()
           └─ BuiltinSystemPromptArgs(...)     [soul/agent.py:324]
        └─ load_agent(agent_file, runtime)     [soul/agent.py:405]
           ├─ load_agent_spec()                [agentspec.py:110]
           │   resolve extend / inherit chain
           ├─ _apply_spec_to_builtin_args()    [soul/agent.py:528]
           └─ _load_system_prompt()            [soul/agent.py:554]
               Jinja2 render with ${KIMI_*}
        └─ KimiSoul.__init__()                 [soul/kimisoul.py:172]
           └─ register injection providers     [soul/kimisoul.py:211]
```
