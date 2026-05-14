# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- **新建 session dialog 显示的是 app 目录而非配置的工作目录**：`GET /api/work-dirs/startup` 端点返回 `os.getcwd()`，但 macOS .app bundle 启动 uvicorn 时的 CWD 是 `~/Library/Application Support/OpenKimo`，导致 "Current" 那项指向用户根本不认识的路径。改为优先读 `KIMI_DEFAULT_WORK_DIR`（OpenKimo wrapper 已在导出，且 sessions.py/admin.py 早已用它做新会话默认目录），未设置时回落到原 `os.getcwd()` 路径。CLI 直接 `kimi web` 行为不变。
- **HEIC/HEIF/AVIF 图片导致对话挂掉**：用户上传 HEIC 图片后，前端把 `data:image/heic;base64,...` 直接发给 LLM，主流 vision 模型（Kimi/OpenAI/Anthropic）一律拒收并把消息留在 conversation history，导致**后续每一轮对话都重发同一张图、每一轮都失败**。子模块改动：
  - 前端 (`web/`)：新增 `lib/heic.ts`，附件入口（drop / paste / picker）统一通过 `heic2any` 把 HEIC/HEIF 转 JPEG 后再进 attachments，缩略图也能正常预览；转码失败时丢弃文件并 toast 提示。
  - 后端 `tools/file/read_media.py`：注册 `pillow-heif` opener，`ReadMediaFile` 工具命中 HEIC/HEIF/AVIF 时主动转 JPEG (q=90)。
  - 后端 `soul/message.py`：新增 `LLM_SAFE_IMAGE_MIMES` 白名单（jpeg/png/webp/gif）和 `sanitize_image_parts()`，在每次 LLM 调用前清洗一份消息副本，HEIC 转 JPEG 失败则替换为 `[image attachment removed: ...]` 文本占位。**只清洗发出去的副本**，原 history 保留用户上传的原图，UI 不受影响。

## [v0.1.3] — 2026-05-10

本版本以"打包稳定性 + 上游同步"为主线：升级 kimi-cli 到 1.41.0，重写 macOS Settings 窗口为单页滚动布局，并把前端 build 接入打包流水线，避免 .app 内 UI 卡在旧版本号。

### Highlights

- **升级到 kimi-cli 1.41.0**：合并 upstream/main 共 21 个上游提交，带来 `afk_mode`、会话遥测、状态栏 agent 计数、headless 剪贴板支持、`/usage` 剩余额度显示修复、OAuth 重连保留 token 等改进。
- **macOS Settings 窗口完全重写**：改用单页滚动 + 原生 Auto Layout，替换之前 `NSStackView` / `NSGridView` / `NSBox` / toolbar 多层套娃，修复多次迭代仍布局错位的问题。
- **打包流水线自动跑前端 build**：`packaging/build.py` 加入 `build_frontend()` 步骤，版本号自动跟随 `pyproject.toml`，避免 .app 内 Web UI 卡在旧版本号。

### Added

#### macOS App 打包（.dmg，从 Unreleased 转正）

- 新增 `packaging/` 目录，可将 OpenKimo 打成自包含的 `.app`，宿主机零依赖。`python packaging/build.py` 输出 `dist/<AppName>.app` + `dist/<AppName>-<ver>.dmg`，拖拽到 Applications 即可使用。
  - 菜单栏常驻状态（`● Running` / `◐ Starting` / `○ Stopped` / `✗ Crashed`），提供 Start/Stop/Restart Server、Open Web UI、Open Admin、Install Package、Open Terminal Here、About、Quit。
  - 原生 PyObjC Settings 窗口（LLM / Web Server / Paths 三个 section）：服务离线时也能改配置，避免"API Key 错 → server 起不来 → 改不了配置"的死循环。
  - 用户可写的 Python 覆盖层 (`PYTHONUSERBASE`)：bundle 内 Frameworks 仍只读签名，用户 `pip install` 落到 `~/Library/Application Support/<AppName>/python-userbase/`，升级 .app 不丢已装包。
  - 白标定制：`--app-name` / `--bundle-id` / `--icon` / `--logo` / `--favicon` / `--brand-name` 全部支持，所有派生路径、CLI 命令、SQLite branding 字段按 `--app-name` 推导，可在同一台 Mac 上并存多个品牌。Web UI 的 logo/品牌名在首次启动自动种入 SQLite，后续 admin 改过的不会被升级覆盖。
  - 文档：`packaging/README.md`、`README.md` / `README.zh.md` 新增 "Option E — macOS App (.dmg)"，`docs/LOCAL-MODE.md` 末尾追加 macOS App 章节。
- CI：新增 `.github/workflows/release.yml`，tag push 时在 macOS runner 上自动构建 `.dmg` 并发布到 GitHub Releases。

#### kimi-cli 1.41.0 升级

- 子模块从原 SHA bump 到 `bc32f372`（合并 upstream/main 之后的 merge commit），吸收社区 21 个 commit，主要内容：
  - `feat(telemetry)`: 会话追踪与 telemetry server，`app_name` / `build_sha` 写入 context provenance。
  - `fix(yolo)`: 解锁 `AskUserQuestion`；新增正交的 `afk_mode`（详见下方 Changed）。
  - `fix(clipboard)`: 在 SSH 接入的 headless Linux 上启用粘贴板。
  - `fix(shell)`: `/usage` 剩余额度显示修复；`/btw` slash command 注册；workflow slash 输入回显。
  - `fix(chat-provider)`: 连接恢复时保留刷新过的 OAuth token。
  - `fix(soul)`: LLM step 重试时清掉残留的 partial UI 输出；上下文 compaction 后重新注入 yolo reminder。
  - `fix(approval)`: pending 请求作用域收敛到 turn 生命周期。
  - `refactor(windows)`: Shell 后端从 PowerShell 切到 git-bash。
- WIP：`LLM_PROVIDERS` 环境变量多 provider 合并 + admin Models tab（`packaging/app_main/configtoml.py`）。

#### 开发工具

- 新增 `scripts/screenshot-settings.py`：Settings 窗口的可视化验证 harness（`--env-fixture` 加载样例配置 + `--out` 输出 PNG），用于截图比对回归。
- 新增 `.claude/agents/kimi-cli-merger.md`：Claude Code 项目级 subagent 定义，专门负责 kimi-cli 子模块与 upstream 的 merge 工作。
- `CLAUDE.md` 加入 task dispatcher 工作模式说明（说明何时把多步任务分派给 subagent）。

### Changed

#### Settings 窗口（完全重写）

- 改用 `NSScrollView` + 纯 `NSView` / Auto Layout，删除原 `NSStackView` / `NSGridView` / `NSBox` / toolbar 套娃实现。
- 单页滚动布局：3 个 section（LLM / Web Server / Paths）+ sticky 底部按钮栏。
- Provider 行用 leading 3pt accent bar 表示分组，移除 `NSBox` 边框。

#### Build pipeline（`packaging/build.py`）

- 加入 `build_frontend(cfg)` 步骤：smart `npm install`（仅在 `package-lock.json` / `node_modules` 过期时跑）+ `npm run build`。
- 新增 `--skip-frontend` flag，便于本地迭代。
- 静态资源选择反转：fresh `web/dist/` 优先于 committed `src/.../web/static/`，避免 .app 内 UI 版本号与 `pyproject.toml` 不同步。

#### kimi-cli 行为变更（来自上游 1.41.0）

- ⚠️ **Breaking**：`yolo_mode` 重命名为 `afk_mode`。`skip_yolo_prompt_injection` → `skip_afk_prompt_injection`，`dynamic_injections/yolo_mode.py` → `afk_mode.py`。下游集成需相应改名。
- `max_steps_per_turn` 默认值由 500 提升到 1000。
- approval 请求不再有 5 分钟超时，转为按 turn 生命周期收敛 pending 集合。

### Fixed

- `.gitignore`：补 `.agents/`，避免大体积第三方 skill bundle（`app/.agents/skills.zip`，~67 MB）误入 git 历史。

## [v0.1.1] — 2026-04-30

post-v0.1.0 patch — README + lint fixes only, no behavior change.

### Added

- README "Option D" 介绍统一启动脚本 `scripts/start.sh`：自动探测 Docker / 本地模式，支持 `--mode`、`--port`、`--host` 参数。

### Fixed

- `web/api/memory.py` 适配 `session.work_dir: str | None`，并对 `json.loads` 的解析结果显式 `cast` 为 `dict[str, Any]`，消除 pyright 报错。
- `memory-panel.tsx` 移除 `void refresh()` 的 `noVoid` 违规，按 biome 建议简化逻辑表达式。
- `sessions.tsx` 给 `MemoryStatusDot` 的 `<span>` 加 `role="img"`，使 `aria-label` 在该角色下合法。

## [v0.1.0] — 2026-04-30

跨会话记忆系统首次上线，是本次发布的最大亮点。同时优化了本地启动体验和镜像分发链路。

### Highlights

- **跨会话记忆系统**：用户首次拥有持久化的"记忆"层，让 LLM 在不同会话之间保留偏好、项目上下文、调研结论。4 类记忆 (`user` / `feedback` / `project` / `reference`) × 3 种触发 (`manual` / `compaction` / `session_end`)。
- **共享知识库 (Knowledge Base)**：项目级、人工策划的 `.kimi/memory/knowledge/index.md` 自动注入系统提示，详细内容放 `wiki/` 子目录由 LLM 按需 `ReadFile`；管理员可在 Admin 面板里直接编辑索引。
- **会话归档异步化 + SSE 实时推送**：原同步的归档（5–30 s 阻塞）改为后台任务，HTTP 立即返回 202，结果通过 Server-Sent Events 实时推到前端，多 Tab 自动同步。
- **侧栏归档状态点**：会话行新增 5 态颜色点（gray / blue 脉冲 / green / yellow / red），让用户能一眼看到哪些会话已归档、哪些有新内容、哪些归档失败。
- **本地模式启动**：新增 `scripts/start.sh --mode=local`，无 Docker 也能直接在宿主机跑。

### Added

#### 跨会话记忆 (Memory System)

- 新增 `kimi_cli.memory` 模块：`MemoryEntry`、`SessionSummary`、文件锁存储；三层结构：
  - **Session 级**：`SessionState.session_memory`，仅当前会话可见。
  - **Persistent**：`<user_dir>/memory/persistent.jsonl`，跨会话，写入需用户审批。
  - **Recent**：`<user_dir>/memory/recent.jsonl`，归档时自动追加，下次会话注入提示。
- 新增 `Memory` 工具（LLM 可 `add` / `list` / `update` / `delete`）。
- 新增 `CrossSessionMemoryInjectionProvider` / `SessionMemoryInjectionProvider` 把记忆拼进 system prompt。
- Web API：`/api/memory/{knowledge,persistent,recent,sessions/{id}/archive,events}`。

#### 共享知识库 (Knowledge Base)

- 新增 `kimi_cli.memory.knowledge`：从 `<work_dir>/.kimi/memory/knowledge/index.md` 加载索引（32 KiB 上限），缺失或为空时安静返回 `None`。
- 默认 agent 系统提示加入 `{% if KIMI_KNOWLEDGE_BASE %}` 模板段，把索引拼进 system prompt 并明确告知 LLM：详细内容在 `wiki/`，按需 `ReadFile`，不要擅自改动 `.kimi/memory/knowledge/`。
- 新增 Admin API：`GET/PUT /api/admin/knowledge/index`，由 `KIMI_DEFAULT_WORK_DIR`（缺省时为 `~`）解析共享目录。
- 前端新增 `AdminKnowledgePanel`（`web/src/features/admin/admin-knowledge-panel.tsx`）：Textarea 编辑器 + 路径展示 + 脏检查 + 保存提示。

#### 异步归档 + SSE

- `POST /api/memory/sessions/{id}/archive` 改返 `202 Accepted`，LLM 摘要在 `asyncio.create_task` 后台跑。
- 新增 `MemoryEventBus`（per-user pub/sub）+ `GET /api/memory/events` SSE 通道。
- SSE 鉴权支持 `?token=` 查询参数（绕过 EventSource 不能发自定义 header 的限制）。
- 多 Tab 同账户实时同步。

#### 前端

- `<MemoryStatusDot>` 在 list / grouped / archived 三处渲染，附 Tooltip 说明状态。
- "Record memory" 菜单项按状态门禁（绿 / 蓝时禁用并弹 `<AlertDialog>` 解释原因）。
- 新增 hooks：`useMemoryEvents`、`useRecentSummaries`、`usePersistentMemory`、`useKnowledgeBase`。
- 60 s 轮询 + `visibilitychange` 监听：让 worker 进程触发的归档 ≤60 s 内被前端感知。
- 90 s 安全超时：服务端崩溃时 in-flight 状态自动转 red。
- 新增 Memory 管理面板（`web/src/features/memory/`）和 Admin 知识库面板。

#### 启动体验

- `scripts/start.sh`：自动检测 Docker；不可用时切到本地模式（`python -m kimi_cli web`）。
- 支持 `--mode=local|docker`、`--port=`、`--host=` 参数。
- 新增 `docs/LOCAL-MODE.md` 详细启动指南。
- `KIMI_OUTPUT_DIR`、`KIMI_DEFAULT_WORK_DIR` 环境变量支持。
- 默认通过 `ghcr.io/j0x7c4/kimi-agent-{gateway,sandbox}` 分发镜像，普通用户不再需要本地构建。
- 新增 `docs/MEMORY-ARCHIVE-FEATURE.md`：记录本次记忆系统的设计、技术路线、踩坑、待优化项。

### Changed

- `docker-compose.yml`：默认 `pull_policy: if_not_present`，避免本地开发误从 registry 拉取覆盖本地构建。
- `Runtime` 增加 `user_memory_dir` 字段，所有读写记忆的代码统一走这里。
- 默认 agent 系统提示要求"始终把输出落到 `/app/output`"。

### Fixed

- 文件面板：长文件名不再把下载按钮挤出可视区。
- 上传文件：服务端重启后不再重发已上传文件（`_sent_files` 持久化到磁盘）。
- `SubagentAnimation` 与 cluster 动画重叠时正确 dismiss；后台 sequential agent 的 cluster 检测；minion 与 hero bot 的 z-index。
- 多个 biome 前端 lint 错误。

### Known Issues

- **跨工作目录的记忆注入未做过滤**：本地 CLI（如 `~/.openkimo`）和容器（`/app`）共享同一份 `recent.jsonl`，可能在容器会话提示里看到本机 work_dir 的旧调研。详见 `docs/MEMORY-ARCHIVE-FEATURE.md` §5.8。
- **worker 触发的自动归档** 需要 ≤60 s 的轮询窗口或切焦点才会在前端可见（gateway 与 worker 是两个进程，未走 SSE 推送，详见 §5.1）。
- **SSE 失败事件不持久化**：服务器重启 / 用户没开 Tab 时归档失败提示会丢，详见 §5.2。

### Upgrade Notes

从 v0.0.1 升级时：

1. **拉取或重建镜像**：
   ```bash
   docker compose pull          # 用 ghcr.io 上的新版镜像
   # 或者本地构建：
   docker compose build
   docker build -f Dockerfile.sandbox -t ghcr.io/j0x7c4/kimi-agent-sandbox:latest .
   ```
   ⚠️ 若 `.env` 自定义了 `SANDBOX_IMAGE`（比如 `kimi-agent-sandbox:latest` 而非 ghcr 全名），记得给新镜像打对应 tag：
   ```bash
   docker tag ghcr.io/j0x7c4/kimi-agent-sandbox:latest kimi-agent-sandbox:latest
   ```
2. **重启已有 session 容器**：旧容器仍跑旧镜像，删掉重建即可使用新代码。
3. **持久化记忆目录**：`KIMI_SHARE_DIR/users/<owner_id>/memory/{persistent,recent}.jsonl` 自动创建，无需手动迁移。

---

## [v0.0.1]

首个版本：基础容器化 agent runtime。

- Docker Compose 一键部署。
- Web UI 会话管理、Multi-LLM (Kimi / OpenAI / Anthropic) 支持。
- 强制 Docker sandbox 隔离每个 session。
- Jupyter Kernel + 无头 Chromium + Shell 工具集。
- 资源限额（CPU / 内存 / 磁盘 / PID cgroups）。
- 危险命令拦截。

[v0.1.3]: https://github.com/j0x7c4/OpenKimo/releases/tag/v0.1.3
[v0.1.1]: https://github.com/j0x7c4/OpenKimo/releases/tag/v0.1.1
[v0.1.0]: https://github.com/j0x7c4/OpenKimo/releases/tag/v0.1.0
[v0.0.1]: https://github.com/j0x7c4/OpenKimo/releases/tag/v0.0.1
