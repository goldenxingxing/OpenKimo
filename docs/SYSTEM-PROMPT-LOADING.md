# 新建 Session 时 Agent System Prompt 加载逻辑

本文档记录在 Web UI / CLI 新建 session 时，`kimo-cli` 后端如何选择 agent、组装并注入 system prompt 的完整链路。所有路径均相对仓库根（`src/kimo_cli/...`）。

## 1. Web 入口

`src/kimo_cli/web/api/sessions.py:489` `create_session()`：

- 接收请求字段 `work_dir` 与可选 `agent_name`
- 若指定 `agent_name`，调用 `discover_user_agent_specs(work_dir)`（`src/kimo_cli/agentspec.py:240`）将名称解析为实际 spec 路径
  - 扫描优先级：项目 `{work_dir}/.kimo/agents/*.yaml` > 用户 `~/.kimo/agents/*.yaml`（项目级覆盖用户级）
  - 自定义 Agent 写盘 / 读取都走这两个目录
- 创建会话：`await KimoCLISession.create(work_dir=work_dir)`（`src/kimo_cli/session.py:158`），生成 UUID 与 `{work_dir}/.kimo/sessions/{session_id}/state.json`

## 2. Agent Spec 加载

`src/kimo_cli/agentspec.py:106` `load_agent_spec()` → `_load_agent_spec()`（递归）：

- 支持 `extend: "default"` 或相对路径继承，按链 merge 配置
- 默认 fallback agent：`src/kimo_cli/agents/default/agent.yaml`
- 系统提示路径相对于 `agent.yaml` 所在目录解析为绝对路径
- 返回 `ResolvedAgentSpec`，含 `system_prompt_path`、`system_prompt_args`、`allowed_skills` / `excluded_skills` 等字段

## 3. YAML Agent Spec vs AGENTS.md

两者是**完全独立的两套机制**，职责不重叠。

| | YAML agent spec | AGENTS.md |
|---|---|---|
| **回答的问题** | "我要启用哪一个 Agent？" | "Agent 在这个项目里要遵循什么规则/上下文？" |
| **位置** | `~/.kimo/agents/*.yaml` 或 `{work_dir}/.kimo/agents/*.yaml` | `{work_dir}` 及其上层目录中的 `.kimo/AGENTS.md` / `AGENTS.md` / `agents.md` |
| **格式** | 结构化 YAML（pydantic schema） | 自由 Markdown |
| **加载入口** | `discover_user_agent_specs()` → `load_agent_spec()` | `load_agents_md(work_dir)` |
| **生命周期** | 新建 session 时**选中一份**作为 agent 定义本体 | 加载所有命中的，与 agent 选择无关 |
| **在 system prompt 中的位置** | 整体是模板来源（`system_prompt_path` 指向的 `.md` 经 Jinja2 渲染） | 被填入模板变量 `${KIMO_AGENTS_MD}`；agent 模板必须主动引用才生效 |
| **继承 / 复用** | 支持 `extend: default` 链式继承 | 不支持继承，按目录层级 root→leaf 拼接 |

### 3.1 YAML agent spec — Agent 的"身份证"

`AgentSpec` schema 字段：

- `name` / `when_to_use` — 名称与触发提示
- `system_prompt_path` — **指向**一个 `.md` 模板文件（不是 `AGENTS.md`），相对 yaml 所在目录解析
- `system_prompt_args` — 传给 Jinja2 模板的自定义变量
- `model` — 默认模型 alias
- `tools` / `allowed_tools` / `exclude_tools` — 内置工具白/黑名单
- `subagents` — 子 agent 路由表（如 default 里挂的 `coder` / `explore` / `plan`）
- `allowed_skills` / `excluded_skills` — Skills 过滤
- `allowed_mcp_servers` / `allowed_mcp_tools` / `excluded_mcp_tools` — MCP 控制
- `extend: default` — 继承另一份 spec，未指定字段从父继承

示例：`src/kimo_cli/agents/default/agent.yaml` 在一份文件里声明 `tools`、`subagents` 和 `system_prompt_path: ./system.md`。

### 3.2 AGENTS.md — 项目级"附加指令"

由 `load_agents_md()`（`src/kimo_cli/soul/agent.py:107`）沿 `project_root → work_dir` 扫描，拼成单串塞进 `KIMO_AGENTS_MD`。它**不定义工具、不定义模型、不影响 agent 路由**，作用近似 `CLAUDE.md`。

**同一目录内的共存规则**：

| 候选 | 关系 |
|---|---|
| `.kimo/AGENTS.md` ↔ `AGENTS.md` | **共存**，都加载，`.kimo/` 在前 |
| `.kimo/AGENTS.md` ↔ `agents.md` | **共存**，都加载，`.kimo/` 在前 |
| `AGENTS.md` ↔ `agents.md` | **互斥**，大写优先（命中后 `break`） |

**跨目录拼接**：按 root → leaf 目录列表逐层收集，最终按 root → leaf 拼接，每段前带 `<!-- From: <path> -->` 注释。越靠近 `work_dir`（越具体）的内容排在越后面，且在 32 KiB 预算分配时享有**叶优先**保护（`agent.py:151-167` 一带），不会被浅层目录的内容挤掉。

### 3.3 易混淆位置

- **`~/.kimo/agents/AGENTS.md` 不会被加载**：`load_agents_md()` 只扫 `work_dir` 路径树，**不会**进入 `~/.kimo/agents/`；同时 `discover_user_agent_specs()` 只 glob `*.yaml`（`agentspec.py:240`），也不会把 `AGENTS.md` 误读成 agent spec。这份文件没有任何加载点。
- **没有"全局 AGENTS.md"机制**：若想把通用指令注入到所有 session 的 system prompt，需要扩展 `load_agents_md` 增加用户级（`~/.kimo/AGENTS.md` 之类）扫描点，框架默认不支持。

## 4. System Prompt 三阶段组装

### 阶段 A — Builtin Args 构造

`src/kimo_cli/soul/agent.py:249` `Runtime.create()` 并发收集上下文，构造 `BuiltinSystemPromptArgs`（字段定义见 `agent.py:60-90`）：

| 模板变量 | 来源 | 备注 |
|---|---|---|
| `KIMO_AGENTS_MD` | `load_agents_md(work_dir)`（`agent.py:107`） | 见 §3.2 的发现与拼接规则 |
| `KIMO_SKILLS` | skills 根目录解析 → `index_skills()` → `format_skills_for_prompt()` | Skills 索引化为提示文本 |
| `KIMO_WORK_DIR_LS` | `list_directory(work_dir)` | |
| `KIMO_WORK_DIR` | 当前工作目录 | |
| `KIMO_OS` / `KIMO_SHELL` | `Environment.detect()` | |
| `KIMO_NOW` | ISO 时间戳 | |
| `KIMO_OUTPUT_DIR` | 环境变量 `KIMO_OUTPUT_DIR`，缺省回退 `/app/output`（`agent.py:416`） | 打包/CLI 启动时应显式设置，否则容器外运行时指向不存在路径 |
| `KIMO_WIKI_CONTEXT` | `build_wiki_context()`（`agent.py:547,574`，渲染「# Knowledge Base」知识库块） | 子 agent 被强制清空（`agent.py:771-773`），知识库仅 root agent 可见 |
| `KIMO_ADDITIONAL_DIRS_INFO` | 附加目录信息 | |
| `KIMO_AGENT_NAME` | 品牌名，默认 `OpenKimo` | |

### 阶段 B — Agent Spec → System Prompt 模板渲染

`src/kimo_cli/soul/agent.py:630` `load_agent()`：

1. `_apply_spec_to_builtin_args()`（`agent.py:761`）：根据 spec 的 `allowed_skills` / `excluded_skills` 过滤 `KIMO_SKILLS` 等字段
2. `_load_system_prompt()`（`agent.py:788`）：Jinja2 渲染（变量分隔符 `${...}`），将 builtin args 与 spec 的 `system_prompt_args` 合并后填入模板
3. 结果由 `KimoSoul` 持久化：`await self._context.write_system_prompt(self._agent.system_prompt)`（`kimosoul.py:2193`）

### 阶段 C — 运行时动态注入

`KimoSoul.__init__()`（`kimosoul.py:477` 一带）注册 `DynamicInjectionProvider`：

- `PlanModeInjectionProvider`
- `TodoSyncInjectionProvider`
- `AfkModeInjectionProvider`（可选，受 `skip_afk_prompt_injection` 配置控制）
- `SessionMemoryInjectionProvider`
- `CrossSessionMemoryInjectionProvider`（**仅 root agent**）

这些 provider **不修改静态 `system_prompt`**，而是在每个 turn 由 `_collect_injections()`（`kimosoul.py:585`）按需拼到消息流。

## 5. 新建 vs 恢复 vs Subagent

| 路径 | 行为 |
|---|---|
| 新建 session | 走完整阶段 A → B → C |
| 恢复 session | 使用相同加载逻辑；system prompt **用当前 spec 重新组装**，不是保存时的快照 |
| Subagent | `Runtime.copy_for_subagent()`（`agent.py:456`）共享父 `builtin_args`、独立 DenwaRenji，再走相同的 `load_agent()`；`KIMO_WIKI_CONTEXT` 被清空 |

## 6. 已知行为与注意点

- **AGENTS.md 截断策略**：总大小超 32 KiB（`_AGENTS_MD_MAX_BYTES`，`agent.py:87`）时浅层目录内容被丢弃以保留深层（项目级）内容。代码内有 `TODO` 考虑将 `KIMO_AGENTS_MD` 挪到 first message 而非 system prompt。
- **Skills 双重过滤**：阶段 B 渲染时按 spec 过滤一次，toolset 加载再过滤一次。
- **`KIMO_OUTPUT_DIR` 默认 `/app/output`**：依赖环境变量注入；未设置时容器外运行会指向不存在路径，打包入口（`packaging/app_main`）与 CLI 启动逻辑负责设置。
- **Subagent 拿不到 cross-session memory 与知识库**：subagent 的 injection provider 不含 `CrossSessionMemoryInjectionProvider`，且 `KIMO_WIKI_CONTEXT` 被清空。
- **恢复 session 不锁定 spec 版本**：用户修改 agent yaml 后恢复旧 session，新的 system prompt 会被使用。

## 7. 调用链速查

```
POST /api/sessions
  └─ create_session()                          [web/api/sessions.py:489]
     ├─ discover_user_agent_specs(work_dir)    [agentspec.py:240]
     │   scan {work_dir}/.kimo/agents/ + ~/.kimo/agents/
     ├─ KimoCLISession.create()                [session.py:158]
     │   mkdir {work_dir}/.kimo/sessions/{id}/state.json
     └─ backend worker
        └─ Runtime.create()                    [soul/agent.py:249]
           ├─ load_agents_md(work_dir)         [soul/agent.py:107]
           ├─ discover/index skills
           ├─ Environment.detect()
           └─ BuiltinSystemPromptArgs(...)     [soul/agent.py:60-90]
        └─ load_agent(agent_file, runtime)     [soul/agent.py:630]
           ├─ load_agent_spec()                [agentspec.py:106]
           │   resolve extend / inherit chain
           ├─ _apply_spec_to_builtin_args()    [soul/agent.py:761]
           └─ _load_system_prompt()            [soul/agent.py:788]
               Jinja2 render with ${KIMO_*}
        └─ KimoSoul.__init__()                 [soul/kimosoul.py:477]
           └─ register injection providers     [soul/kimosoul.py:477-490]
```
