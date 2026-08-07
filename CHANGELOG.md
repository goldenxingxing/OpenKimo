# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [v0.1.30] — 2026-08-07

### Fixed

- **Windows 打开会话即 `worker process exited unexpectedly (exit code 1)`（v0.1.29 引入的回归）**：v0.1.29 为修 `.env` 的 GBK 问题给整个进程树设了 `PYTHONUTF8=1`。它不只影响本进程的读写——它还会改掉 `locale.getpreferredencoding()`，而那正是 `subprocess(text=True)` 用来解码子进程输出的编码。中文 Windows 上 `where.exe git` 找不到时会打印 `信息: 用提供的模式无法找到文件。`（GBK，首字节 `0xD0`），于是解码在 subprocess **自己的 reader 线程**里抛 `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd0 in position 0`——那个位置调用方根本 catch 不到。git 探测因此拿不到任何结果，退化成"没装 git-bash"，`Runtime.create` 抛 `GitBashNotFoundError`，worker 在用户开口之前就退出了。现在**不再设 `PYTHONUTF8`**；`PYTHONIOENCODING=utf-8` 保留——它只管本进程的 std 流（父进程正是用 `.decode("utf-8")` 读 worker 的 stderr），不影响子进程解码。v0.1.29 真正要修的 `.env` 编码问题不依赖 `PYTHONUTF8`，那部分是显式 `encoding="utf-8"`，不受本次回退影响。
- **git-bash 探测不再会被一行读不懂的输出干掉**：上面那条的第二层原因是探测本身太脆。`where.exe git` 和 `git --exec-path` 的输出现在按 `errors="replace"` 解码——路径乱码是可恢复的，探测器死掉不是。即使今后有人在环境里设了 `PYTHONUTF8`，也不会再连锁到 worker 启动失败。

## [v0.1.29] — 2026-08-06

### Fixed

- **Windows 配置 Provider 报 `gbk codec can't encode character '\xb3'`**：`.env` 的读写是整个桌面端唯一没写明编码的文件 IO，取的是系统默认值——macOS 上是 UTF-8 所以看不出来，中文 Windows 上是 cp936，于是配置里只要有一个 GBK 装不下的字符（报错里的 `\xb3` 就是 `³`），保存就直接失败，而 `save_env` 会把写入异常原样包成 `Failed to write .env: {e}` 抛回界面。跟 provider 本身无关，只是那里恰好粘进了这样一个字符。现在一律按 UTF-8 写入；旧版本用 ANSI 代码页写下的 `.env` 仍能读出并在下次保存时归一，不会让老用户升级后配置读空（回退编码写死 `mbcs` 而非 `locale.getpreferredencoding()`，因为下面这条改动会让后者在最需要回退的机器上返回 UTF-8）。同时 launcher 与 uvicorn supervisor 设 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`，整棵进程树不再走 ANSI 代码页——此前一行日志或一段模型输出里出现 GBK 没有的字符，就会在离用户操作很远的地方抛 UnicodeEncodeError，文件工具也在按 GBK 解码工作区文件。
- **Windows 上传文件后对话历史里没有文件图标**：服务端用宿主机真实路径标识上传文件（`<uploaded_files>` 列表和 image/video/document 标签的 `path=`），Windows 上就是 `C:\...\<session>\uploads\report.pdf`，而前端四处都假定 POSIX 路径——靠开头的 `/` 判断这行是不是文件路径、用 `split("/")` 取文件名、正则里硬编码 `uploads` 两侧的正斜杠。第一条让整行既不生成附件又被当普通文本，第二条把整条路径当成文件名、拼出的地址必然 404。服务端不能改成正斜杠（agent 需要原生路径打开文件），因此在读取侧归一化，Windows 绝对路径、UNC、legacy 相对形式都认，同时把"是不是上传路径"的判断收紧而非放宽——这些行会从用户看到的文本里删掉，误判就是吃掉用户自己写的内容。这也是为什么它在 deepseek 会话里被发现：无 vision 能力的模型走不到 `<image>` 分支，只剩 `<uploaded_files>` 这一条路，而那正是被反斜杠打断的那条。
- **跨会话记忆在上下文压缩后彻底消失**：快照只在首步注入一次并缓存，而它本身是一条普通历史消息，压缩会把它折进压缩摘要——由于一次性标志仍然置位，此后整个会话再也不会重新注入。`DynamicInjectionProvider` 早就为此留了 `on_context_compacted` 钩子，这个 provider 只是从没覆写。提示词缓存零成本（压缩按定义已作废整个前缀）。这大概也正是模型反复重记同一件事的原因：它在第 1 步看过一次记忆池，等到决定某件事值得留存时，那段快照早已滚出视野。

### Changed

- **跨会话记忆自动去重**：`persistent.jsonl` 此前无上限、无过期、无去重，且每次会话启动全量注入——一条事实记两遍，之后每次对话都要为它付费；唯一的约束是提示词里一句"别重复记"。现在重述一条已存在的事实会**原地更新**该条目而不是新增一条，存量重复也会被下一次写入顺带折叠。难点在于拒绝合并"看起来像"的两条：`difflib` 量的是字符重合度，而它分不清改写和更正——`"Merge freeze begins 2026-03-05"` 对 `"…begins on 2026-03-05"`（该合并）与对 `"…begins 2026-04-05"`（绝不能合并）**得分完全相同，都是 0.967**。所以合并除了要过阈值，还要过三道与阈值正交的守卫：长度比（合并只丢弃旧文本，危险形状是详细原文被一句敷衍重述覆盖）、数字 token 多重集相等（日期、版本、配额、工单号；比的是多重集而非位置，所以插入一个介词仍能合并）、否定极性一致。阈值与守卫朝不同方向失效，阈值设错也不会单独造成事实丢失。相似但未达标的条目不会被动，而是连同内容一起报告回来。`kind` 是硬分区：同一句话记作 `user` 和记作 `feedback` 含义不同，跨 kind 只提示不合并。合并保留旧 id 与 `created_at`，因此已经拿到 id 的更新/删除不会失效。合并时的确认提示会并列展示新旧两段文本，并使用独立的授权动作——允许 agent **创建**条目不等于允许它**覆盖**已有条目。存量压缩只做精确匹配，绝不模糊：模糊合并只施加于当前这次写入，此时用户在场。
- **同一个会话在"最近会话"里只占一格**：一个会话此前每次上下文压缩写一条摘要、结束时再写一条，而压缩摘要是累积的（下一次总结的历史开头就是上一次的摘要），所以一个长对话就能占掉二十格里的大半、把别人挤出去。现在同会话的记录收敛为最新一条。升级后首次写入时存量会明显缩短，这是预期效果。

## [v0.1.28] — 2026-08-05

### Added

- **自动压缩阈值可在 Settings 里调，且不重启就生效**：`compaction_trigger_ratio` 早就在 `config.toml` 里，却没有任何设置入口，而其他全局配置一律靠重启 worker 生效——正在跑的任务会被打断。它不需要如此：KimiSoul 每一步都从 config 对象重新读取该比例，而不是构造时快照，因此原地改写下一步即可读到，轮次中途也算。新增 `set_compaction_ratio` 消息（比照既有的 `set_plan_mode`），由 runner 推给每个在线 worker；`GET /api/config` 暴露该值，`PATCH` 接受它，只改比例的请求不再重启任何东西，响应改为报告哪些在线会话被原地重新调参（改模型或 thinking 模式仍旧重启）。已停止的会话无需消息，下次启动时读持久化值。取值范围在 config 字段、wire 参数、请求模型三处分别校验，越界值到不了运行中的 agent。
- **Settings 窗口的 Auto-compact 滑杆**：跨度 0.5–0.99，与 kimi-cli 加载时校验的区间一致，任何能拖到的位置都不会产出一个应用随后拒绝启动的配置。它是本窗口唯一属于 `config.toml` 而非 `.env` 的值，因此不走 `_collect_updates`，由 `_save` 经 `configtoml` 直接写入；读回当前值是防御式的——文件缺失、段缺失、值越界、toml 解析失败都退回默认值，而不是让窗口打不开。写入侧同样拒绝区间外的值：kimi-cli 加载 `config.toml` 时校验同一区间，写进去只会把一个坏的滑杆位置变成一次被归咎于配置文件的启动失败，在写入处失败才指得出真正的责任方。
- **滑杆旁显示当前值**：Temperature 和 Auto-compact 此前都是裸滑杆，拖得动却分不清 0.72 和 0.78，而这两个值要与默认值比较、还要写进配置文件。右侧加定宽读数，拖动过程中经 `setContinuous_` 实时更新。比例以百分比呈现——"auto compact at 80%" 本就是人看它时的语义；temperature 仍是两位小数。初始值与实时更新走同一个格式化函数，打开时看到的和拖动时看到的不会漂移。

### Changed

- **全局 Wiki 改为按需检索**：使用中的反馈是，同样的事情交给 `AGENTS.md` 很顺——该触发时才触发，而现在每一轮对话都要过一遍 Wiki。差别在于是模型判断这次查询值不值，还是运行时无条件搜索；后者让大多数本不需要它的对话付出了延迟和提示词空间。索引本就在系统提示里，agent 看得见 Wiki 里有什么，需要时自己调 `Wiki(operation="search")`——就是 `AGENTS.md` 的形态与同样的可供性。轮次跟踪、显式意图识别、evidence 采集均不变，去掉的只是那次无条件搜索。此外：检索前先读修订标记，全新安装下的必然落空的全文查询直接跳过（排在既有跳过检查之后，合成提示与斜杠命令仍会报告跳过原因；读标记时任何异常都按"非空"处理，照原路搜索）。
- **checkpoint 不再让每次对话多付一到两次模型调用**：投递 checkpoint 会追加消息并继续循环，本身就是一次 completion，模型不解决还要再来一次；而只要一轮产生了触发性 evidence 和看起来可复用的结论就会创建 `root_evidence` checkpoint——几乎每一轮读文件的对话都符合。现在只有 `explicit_user_durable`（用户明确要求留存的知识）才打断循环，evidence 类 checkpoint 是机会而非义务，照旧随轮次退休，不走往返。这也让循环重新符合设计本身"不对每个工具结果、日常对话、状态与进度采取行动"的约束。

### Fixed

- **Wiki 写入被模型整体放弃（"工具还是不行"）**：真实会话里 agent 转而去写普通文件。它没撞上 bug，而是撞上了一个不可能满足的要求：checkpoint 块按路径列出来源，持久化要求候选内容携带与运行时观测逐字节一致的 `SourceRef`——正确的 workspace id、正确的内容哈希，而 agent 无从得知任何一个。它在 transcript 里的原话是"计算文件 sha256 很麻烦……这变复杂了"，然后调用了 discard。讽刺的是本就已确立：模型提供的来源哈希对授权毫无贡献，只有运行时自己的 evidence 算数，所以要求模型复述这些哈希是零安全收益的摩擦。改为模型提供内容、运行时从该 checkpoint 自身的 evidence 提供来源。自行列出来源的候选不受影响，仍走完整校验——编造的来源照旧被拒，不含 evidence 的 checkpoint 也贡献不出来源，无来源的候选依然无据可依。
- **WebSocket 抖动误报，空闲掉线却不重连**：任何 error 事件都弹 toast，而该事件按设计不带细节，于是提示只有一句"WebSocket connection error"，且客户端片刻后已自行恢复它也照弹；`onopen` 会清掉错误状态，但 toast 收不回来，界面就一直在为一条好好的连接报错。底下还藏着一件事：意外关闭根本没有重试，只有 watchdog 会重连且只在流式输出期间——空闲时断掉的 socket 会一直断到页面刷新。只压掉 toast 会把这个问题一并盖住，所以重试与上报在 `connection-policy.ts` 里一起决策：异常关闭按封顶指数退避重试；服务端用来拒绝会话的关闭码（4004、4029）立即上报，因为重试只会重复它；正常关闭或客户端主动关闭则忽略。连续五次失败后读者才会收到提示，且说的是"连接已丢失且无法恢复"，而不是念一个不透明的事件名。十三个测试覆盖该策略，包含引出这次反馈的抖动场景和服务端真的没了的场景。

## [v0.1.27] — 2026-08-03

### Fixed

- **Intel Mac 装不上（较低版本 macOS）**：`LSMinimumSystemVersion` 被设为 14.0（Sonoma），而多数 Intel 机型最高只能升到 Monterey 12 / Ventura 13，系统直接拒绝安装。实测捆绑的 CPython 与全部 195 个原生扩展都只要求 macOS 11.0，14.0 完全来自 `brand.toml` 的人为设定（同时喂给 Info.plist 和 launcher 的 `-mmacosx-version-min`）。下调至 11.0，Big Sur 及以上的 Intel Mac 均可安装。构建流程新增 `verify_min_macos()`：装配后、签名前扫描 bundle 内每个 Mach-O，任一二进制要求高于声明值即中断构建并列出具体文件——声称支持却跑不起来，比装不上更难排查。
- **聊天视图偶发跳到历史最上方**：重连或 worker 重启会清空消息列表并分批回放历史。原有的"追随底部"标志在视口第一次到达底部时就被清除，而回放初期只有寥寥几条消息、视口天然就在底部，于是标志提前失效；随后涌入的数百条消息把内容高度撑过 1500px 容差，跟随规则放弃，视口就停在了巨大列表的开头。改为仅当读者**自己**滚动（wheel / touch / 键盘）时才交还控制权，列表增长永远不会提前结束追随。
- **Tasks 进度显示滞后且不随新任务重置**：面板此前只在切换会话时清空，同一会话内发起新任务时旧清单继续挂着，若新任务未拆分则残留至会话结束；重开会话时回放会按序应用每个 todo block，导致显示的可能是很早以前那一轮的进度。改为在 `TurnBegin` 清空（实时与回放均生效）。同时明确 SetTodoList 的更新节奏：开始前标 `in_progress`，完成的瞬间在标记下一项的同一次调用里标 `done`，减少轮次内的显示滞后。

### Added

- **全局 Wiki 确定性运行时触发**：写入权限不再建立在模型的自我声明上，而是由运行时实际观测推导。运行时观察真实工具返回生成只含哈希的 evidence；evidence 加结论形成 checkpoint（机会而非许可）；root agent 必须对每个 checkpoint 明确 persist 或 discard；persist 需换取一次性 grant，且会重新读取并比对每个来源。模型提供的 `value="high"`、复述的哈希、自编的 checkpoint id 一律不参与授权。子模块可贡献 evidence 但零 Wiki 权限（无 manager／tool context／coordinator，提示词中 Wiki 段落清空，工具本身 root-only 兜底）。含每轮受控检索、单次提醒的解析循环、按事件白名单过滤的隐私安全遥测，以及跨平台与预算约束验证。

## [v0.1.26] — 2026-07-31

### Changed

- **登录页品牌文案**：将登录页副标题中的 “Kimi Code” 更新为 “OpenKimo”。

## [v0.1.24] — 2026-07-25

### Fixed

- **Windows worker 启动即崩（InvalidToolError: InstallSkill）**：`skill_install.py` 的 `from __future__ import annotations` 让 `Runtime` 注解变成字符串，依赖注入查找失败，一个工具加载失败杀死整个 worker。kimi-cli 端改用 `inspect.signature(eval_str=True)` 解析注解，且缺依赖的工具降级为"跳过 + 警告"而不再让 worker 崩溃。
- **mac 端 `Could not find platform dependent libraries <exec_prefix>` 警告**：CI 构建把 runner 的绝对路径烤进各 venvstacks 层的 `pyvenv.cfg` / `sitecustomize.py`，装到用户机器上路径失效，每次启动捆绑 Python 都向 server.log 打警告。app_main 启动时检测失效的 `home` 并原地重跑该层的 postinstall 生成逻辑（跳过字节码预编译）自愈。
- **Windows 右键 Settings 弹出 python.exe 控制台窗口**：Settings 子进程改用 `pythonw.exe` + `CREATE_NO_WINDOW` 启动。
- **Windows 卸载报 "cannot call WizardIsTaskSelected"**：该函数在卸载阶段不可用（"同时清除用户数据"选项因此从未生效）。安装完成时把勾选结果落成 marker 文件，卸载时读回执行。

### Changed

- **会话中的身份跟随品牌**：agent 自我介绍从写死的 "Kimi Code CLI" 改为渲染 `KIMI_AGENT_NAME` 模板变量；桌面端把品牌名（OpenKimo 或白标 `--app-name`）通过环境变量注入。
- **Windows Settings 页面与 mac 版对齐**：多 Provider 编辑（名称/类型/API Key/Base URL/模型/最大上下文/capabilities/默认项）、Thinking 与 Temperature、Session Token 生成、LAN Only、品牌重置，保存语义（`LLM_PROVIDERS` 写入、legacy 键清理、`config.toml` 同步、LLM 变更自动升级为重启)与 mac 一致，并新增从 mac 源码直接取常量做对比的 parity 测试。

## [v0.1.4] — 2026-05-16

本版本以"会话定制 + 配置闭环 + HEIC 兼容性收尾"为主线：新增自定义 Agent 启动会话功能，把 macOS Settings 写入 `.env` 后真正同步到 `config.toml`（之前 base_url/api_key 改了没生效），并补齐 HEIC 上传链路上的两处兜底。

### Highlights

- **自定义 Agent — 启动会话时挂载用户 spec**：可以从 `~/.kimi/agents/` 或 `<work_dir>/.kimi/agents/` 加载 YAML spec，精确控制可用工具、skills、MCP 权限、是否注入知识库、系统提示词。Web 新建会话对话框「高级设置」新增 Agent 下拉框；CLI `--agent <name>` 也走同一发现链。详见 `docs/CUSTOM-AGENTS.md`。
- **Settings 写盘真正生效**：之前在 macOS Settings 窗口里改 provider 的 `base_url` / `api_key` 只更新 `.env`，但 `config.toml` 中已有的 `[providers.X]` / `[models.X]` 表项被 `_build_global_config` 优先吃掉，导致编辑被"静默吞掉"。这版补齐了 `configtoml.update_providers` / `update_models` / `set_default_model`，保存时按 .env 上 LLM_PROVIDERS 重新 upsert 这些表项，保留手写注释和无关字段。

### Added

- **`skills/grounding-dino-seg2`**：基于 Grounding DINO 的图像分割 + 背景替换 skill。
- **CHANGELOG**：本条目。

### Fixed

- **新建 session dialog 显示的是 app 目录而非配置的工作目录**：`GET /api/work-dirs/startup` 端点返回 `os.getcwd()`，但 macOS .app bundle 启动 uvicorn 时的 CWD 是 `~/Library/Application Support/OpenKimo`，导致 "Current" 那项指向用户根本不认识的路径。改为优先读 `KIMI_DEFAULT_WORK_DIR`（OpenKimo wrapper 已在导出，且 sessions.py/admin.py 早已用它做新会话默认目录），未设置时回落到原 `os.getcwd()` 路径。CLI 直接 `kimi web` 行为不变。
- **HEIC/HEIF/AVIF 图片导致对话挂掉**：用户上传 HEIC 图片后，前端把 `data:image/heic;base64,...` 直接发给 LLM，主流 vision 模型（Kimi/OpenAI/Anthropic）一律拒收并把消息留在 conversation history，导致**后续每一轮对话都重发同一张图、每一轮都失败**。子模块改动：
  - 前端 (`web/`)：新增 `lib/heic.ts`，附件入口（drop / paste / picker）统一通过 `heic2any` 把 HEIC/HEIF 转 JPEG 后再进 attachments，缩略图也能正常预览；**转码失败时不再丢文件**，把原 HEIC 透传给服务端 fallback。
  - 后端 `tools/file/read_media.py`：注册 `pillow-heif` opener，`ReadMediaFile` 工具命中 HEIC/HEIF/AVIF 时主动转 JPEG (q=90)。
  - 后端 `soul/message.py`：新增 `LLM_SAFE_IMAGE_MIMES` 白名单（jpeg/png/webp/gif）和 `sanitize_image_parts()`，在每次 LLM 调用前清洗一份消息副本，HEIC 转 JPEG 失败则替换为 `[image attachment removed: ...]` 文本占位。**只清洗发出去的副本**，原 history 保留用户上传的原图，UI 不受影响。
  - 后端 `web/runner/process.py`：上传附件入口同样注册 `pillow_heif` opener（之前只在 `read_media.py` 注册，Web 直传走的是 `process.py` 这条路），并把原先吞掉所有图片编码异常的 bare `except` 改成 `logger.exception`，方便定位失败原因。

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
