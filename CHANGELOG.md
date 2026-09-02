# Changelog

All notable changes to this project are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [v0.1.41] — 2026-09-02

一次全项目审查后的修复集。60 个首方文件、约 14.3k 行，由三条互相独立的路径各审一遍（内置 code review、OpenCodeReview、以及一批无上下文的独立 agent），取交叉命中和逐条复现后的结果。

### Security

- **一个 sandbox 可以在另一个 sandbox 的会话里执行任意代码**。`JupyterKernel` 用 `KernelManager(ip=get_host_ip())` 把 kernel 的五个 ZMQ socket 绑到了容器的可路由桥接 IP 上——而那个函数的兜底是 `except: return "0.0.0.0"`，即绑所有网卡。jupyter_client 的默认是 loopback，它自己的文档就在这个参数上写了警告。

  单独看这还只是暴露面。真正成链的是另一端：kernel 管理服务监听 `0.0.0.0:8888`，而 `/kernel/connection` **无鉴权**返回完整的 connection file，**包含 HMAC 签名 key**。于是同一个 `kimi-agent-network` 上的 sandbox A 可以读到 B 的 key 和端口，签一个 `execute_request` 发到 B 的 shell 通道，在**另一个用户的会话里**跑代码。

  现在 kernel 和管理服务都绑回环，`/kernel/connection` 打码签名 key，`/kernel/debug`（docstring 一直写着"开发用"却无条件注册）改为 `KERNEL_SERVER_DEBUG=1` 才启用，CORS 去掉 `allow_credentials`——它配着通配符 origin 本来就被浏览器拒绝，开着只是让一个无鉴权 API 看起来更开放。

- **打包脚本对 bundled CPython 不做校验**。busybox 有 `BUSYBOX_SHA256` 且强制比对，而 python-build-standalone 的 tarball 只是算出 SHA256 打印出来、**不与任何值比较**，然后无过滤 `tar.extractall()`。同时 `release.yml` 缓存 `packaging/.cache`，而 GitHub Actions 的缓存对该仓库**任意分支**的 workflow 可写。现在钉住校验和，不匹配即报错并删除缓存副本，解包改用 `filter="data"`。

- **生成的 shell 垫片未转义安装路径**。`userenv` 把 `PYTHONUSERBASE` 等路径用裸双引号插进 `#!/bin/sh` 脚本，路径里的反引号或 `$(...)` 会在用户每次调用 `python`/`pip` 时被执行。改用 `shlex.quote`。

### Fixed

- **Windows 安装包缺少 carryover，启动即崩**。`carryover` 曾是 `src/kimi_cli/` 里的 vendored 副本，随源码复制进 runtime；成为 uv workspace 成员后它只存在于工作区、从未发布到 PyPI，于是只看到 kimi-cli wheel 的那次 pip resolve 会去 PyPI 找它、拿到 404、什么都不装——而构建**只打了一行 warning**。装好的应用在 `kimi_cli/utils/string.py` 第 7 行抛 `ModuleNotFoundError: No module named 'carryover'`。

  现在从声明它们的文件读出全部 workspace 成员、逐个构建 wheel，并与 `requirements-windows.txt` 合进**同一次 pip resolve**。必须同一次：只有作为直接需求，本地的 carryover wheel 才能满足 kimi-cli 的依赖。`requirements-windows.txt` 一起进来则是反过来的道理——作为第二次 `--upgrade` 安装打进同一个 `--target`，它的 `Pillow>=10.0` 会悄悄覆盖 kimi-cli 钉住的 `pillow==12.2.0`。

- **Docker 部署路径三处独立断裂**。compose 指向 `ghcr.io/j0x7c4/*` 而 release 实际发布的是 `ghcr.io/goldenxingxing/*`；`deploy.sh` 往 `.env` 写的 sandbox 镜像是裸本地 tag，而 compose 只定义 gateway 一个服务、CI 只构建 gateway，`Dockerfile.sandbox` 全仓库只在 `release.yml` 出现一次，所以那个镜像**没有任何人构建或拉取**，每个会话都以 `No such image` 失败；`KIMI_SESSION_DATA_DIR` 默认相对路径，而 gateway 把它原样交给 `docker run -v`，docker CLI 按**客户端 cwd** 解析——也就是 gateway 容器内的 `/app`，于是每个 sandbox 挂到的是宿主机上另一个空目录，worker 看不见会话数据且不报错。

- **首次安装既不弹设置窗口也不启动服务器**。`_startup()` 跑在工作线程上，而 `rumps.Timer.start()` 把 NSTimer 挂到该线程的 run loop——那个 loop 从不运行，随守护线程一起消失。同一文件的 `_on_state_change` 早就用主队列并写了注释说明这个坑，首次安装这条路径没用上。

- **服务器监管的三处竞态**：崩溃退避期间点「Stop Server」，两秒后看门狗仍会拉起新进程；重启时旧进程迟到的退出码会清掉对**新**进程的引用，监管器随后重复 spawn、发现端口被自己刚遗弃的进程占用、报 CRASHED，而那个无人监管的服务器还在跑；`_spawn_locked` 抛异常会直接打死看门狗线程，菜单栏永远停在「Running」而服务器并不存在。

- **Settings 保存会毁掉 config.toml**。默认模型名被小写化后当作精确 TOML key 写回，导致 `prune` 删掉 `[models.MyLocal]` 并写下 `default_model = "mylocal"`，此后每次 `load_config` 都抛 `Default model mylocal not found in models`，服务器起不来。

- **CDP 命令按固定 id 配对**。每条请求都写死 `"id": 1`，超时命令的回复不排空、连接继续复用，于是迟到的回复被当成**下一条**命令的结果。已用真实 Chromium 复现并验证修复。

- 另有二十余项：`packaging/__init__.py` 在没有 PyPI `packaging` 的解释器上抛 `NameError` 导致 Windows launcher 无法 import；`launcher.c` 定义了 `CPYTHON_PREFIX` 却四处硬编码 `3.12`；macOS 构建的 wheel 排序对 `pykaos` 永远失效（它的目录名是 `kaos`），使依赖排在依赖者之后；`verify_min_macos` 按后缀过滤，从未检查过 bundled 解释器本身；`.env` 未加引号而 `start.sh` 会 `source` 它；kernel 执行循环里一个永远不成立的字符串比较使每次超时都重启 kernel、清掉用户状态。

### Changed

- **发布流水线**：四个 job 竞相创建同一个 Release（其一会拿到 HTTP 422），改为单独一个 `create-release` job 由其余四个依赖；第三方 action 从 `@main` 移动分支钉到具体 commit（该 job 持有 `packages: write` 和 `RELEASES_TOKEN`）；补齐 `permissions` 与 `timeout-minutes`。
- **CI 新增一步 lint**，覆盖 `app/`、`packaging/`、本仓库 `tests/` ——此前 ruff 只跑 kimi-cli 子模块的 `src tests benchmarks`，这些目录从未被检查。`packaging/app_main/settings_window.py` 有 1500 行且没有任何测试 import 它（全部引用都是读文本做匹配），一个未定义名字整套测试照样全绿。
- **`deploy.sh` 不再询问不生效的限额**：`SANDBOX_DISK_LIMIT`、`MAX_SESSION_CONTAINERS`、`SANDBOX_TIMEOUT_SECONDS` 在代码里没有任何消费者，`container.py` 只施加 `--cpus`/`--memory`/`--pids-limit`。问了却不生效比不问危险。

### 已知未验证

macOS 安装包已本地构建并验证。Windows 打包、Docker 部署、以及重构后的 `release.yml` 本身，均以本次发布为首次真实执行。多架构（arm64）镜像仍未提供。


## [v0.1.40] — 2026-09-01

### Fixed

- **全局默认模型重新可以设置了**。v0.1.39 加入会话级模型时，把原来的全局切换**顶掉了**而不是并存：输入框那个选择器过去写的是 `config.defaultModel`，会重启空闲会话让它们用上新模型、报告跳过的忙碌会话、并提供「强制重启」；改动之后它只写会话级覆盖。

  别处也补不上——Admin → Models 只显示一个 `Default` 徽章，**背后没有任何动作**，而且全仓库没有任何组件再调用全局配置的 update。于是 v0.1.39 发出去的版本里，**界面上根本改不了默认模型**。

  成因是那个会话级功能的提交写于一个月前，针对的是这个组件的旧版本并把它整体重写了，所以是"取代"而非"合并"；而且取代发生在**不冲突的代码块**里，没有任何冲突标记提示有东西被删掉。

  现在：选模型仍然绑定当前会话；把它设为全局默认是选择器底部一个**独立的动作**——影响面是所有没有自己模型的会话，值得单独一步。重启汇报和「强制重启忙碌会话」按钮一并回来。选择器的文案原先通篇写着"全局"而已名不副实，现在改为「模型」。

  **仅影响 v0.1.39**，该版本已标记下架。

## [v0.1.39] — 2026-09-01

### Added

- **一个会话可以自己选模型**。此前模型是全局的——`config` 里一个 `default_model`，改了对所有会话生效。想在某一条线上试更便宜或更强的模型，就得把整个工作区切过去，事后还要记得切回来。

  现在会话可以带自己的模型，存在该会话的 `session_config.json` 里。这个文件**本来就在放会话级覆盖**（`thinking`、agent spec 路径），所以是顺着已有形状走的，没有新开一处地方。worker 通过 `read_session_overrides()` 读整张覆盖表，缺什么就回落到全局配置——没有覆盖的会话行为不变，包括全局默认之后被改动时仍然跟随。

  `Session` 和 `UpdateSessionRequest` 各加一个可空的 `model`：`null` 表示"跟随全局默认"而不是"没有模型"，这正是让一个被钉住的会话能交还给默认值的原因。

  选择器和 plan / YOLO 控件放在一起，显示会话自己的模型，没有则显示全局默认。**生成过程中拒绝切换**并给出提示——模型是在一轮开始时读取的，中途切换会静默不生效。尚未创建会话的输入框会把选择暂存为草稿，创建时再应用。

  测试见 `kimi-cli/tests/web/test_session_model_override.py`。

## [v0.1.38] — 2026-09-01

### Fixed

- **一个不读数据的浏览器 tab 不再能把整个 session 冻住**。症状是 session 看着还停在半路，刷新一下才发现它早就把活干完、输出也结束了（复现于 session `30255445-d66c-44c9-be27-46074a7ea917`：worker 在 16:22:29 写完 `TurnEnd`，页面直到 16:40 手动刷新才看到）。

  根因在 `web/runner/process.py`：`_broadcast()` 是在**读 worker stdout 的那个 task 里**逐个 `await ws.send_text()` 的。于是一条链路串起来了——某个客户端停止读它那条 socket（后台 tab、渲染线程卡住、机器睡眠、半死的连接）→ 该 socket 的 `send_text` 永远不返回 → `_broadcast` 卡住 → 读 stdout 的循环停止排空 → worker 的 stdout 管道（64KB）写满 → worker 的 `_write_loop` 卡在 `drain()`。

  关键是 **agent 本身没有停**：`run_soul` 往 `wire.jsonl` 写文件走的是另一条路，完全不受影响。所以文件里一切齐全，而实时画面停在最后一条挤进管道的消息上——**刷新之所以能"修好"，正是因为刷新关掉了那条卡死的 socket，把阻塞的 `send_text` 释放了**。日志里的旁证：8/31 三次自动重连（15:45:53 / 15:49:03 / 15:51:53）之间隔着约 3 分钟，正好是"重放完整个 wire.jsonl 追上进度 → 再次卡住 → 前端 45 秒看门狗超时"的周期。

  现在每条连接有自己的发送队列和写任务，`_broadcast` 只入队、从不阻塞；背压被关在它该待的那一条连接里。掉队太多（8192 条 / 64MB）或单帧 60 秒发不出去的客户端会被摘掉并以 1011 关闭，前端照常重连并从 `wire.jsonl` 重放——也就是说**过去要靠人手刷新的那一下，现在服务端在 60 秒内自己做了**。关闭握手本身也可能卡在同样的原因上，所以它是甩到后台带超时跑的，绝不让调用方等。

  回归测试见 `kimi-cli/tests/web/test_websocket_backpressure.py`（修复前 5 条里挂 3 条）。

- **装好的 app 不再依赖打包它的那棵仓库树**。venvstacks 的 `postinstall.py` 是在构建目录里跑的，它把当时的绝对路径写进了每层的 `pyvenv.cfg` 和 `sitecustomize.py`；`.app` 拖进 `/Applications` 之后，这些路径**还指着仓库里的 `dist/`**。证据是装好的应用自己的 `sys.path`：

      /Applications/OpenKimo.app/.../app-app-openkimo/lib/python3.12/site-packages
      /Users/…/OpenKimo/dist/OpenKimo.app/.../framework-framework-kimi/…      ← 指回仓库
      /Users/…/OpenKimo/dist/OpenKimo.app/.../cpython-3.12/…/lib-dynload      ← 也是

  只要仓库被移走、清理，或者下一次构建把 `dist/` 删掉重建，装好的应用就再也起不来（`No module named 'acp'`，框架层看不见了）。它之所以一直没被发现，是因为**只要构建目录还在，一切看起来都正常**。

  全包里只有 4 个文件带着这些路径，每层两个。现在两个都改成相对自身位置：`pyvenv.cfg` 的 `home` / `executable` 用相对路径（CPython 是相对 venv 目录解析的，不是相对 cwd，已在三个不同 cwd 下验证），`sitecustomize.py` 重新生成成从 `__file__` 推算同级层，目录清单读 venvstacks 自己那份层元数据。匹配用 `runtimes/` 作锚点而不是直接 relpath：记录的路径若不属于这棵树就不动它，让下面那道检查把构建拦住，而不是生成一串指向未知位置的 `../../..`。

  **代价说清楚**：相对 `home` 会让解释器每次启动往 stderr 打一行 `Could not find platform dependent libraries <exec_prefix>` —— 发出这行的探测跑在相对路径被解析之前，之后三层依然全部正确落到 `sys.path`（ssl / sqlite3 / socket / select / zlib 均验证可导入）。另一条路是按 venvstacks 的设计在安装后重跑 `postinstall.py`，没有这行警告，代价是往已签名的 bundle 里写文件，毁掉 DMG 带的那份签名。

### Added

- **构建期检查 `verify_relocatable`，在签名前跑**。扫 `runtimes/` 下的文本文件，只要还出现 bundle 自身路径或仓库根目录就让构建失败。第一次跑它就抓到了东西——不过抓的是 `*.dist-info/direct_url.json`，那是 PEP 610 的安装来源记录，按定义就是构建机路径，且 import 时无人读取。已排除：**因为一份无害的元数据而让构建红掉，只会教会大家跳过这道检查**。

### Fixed

- **不会再每天早上被踢回登录页**。会话过期是从登录那一刻钉死的 24 小时窗口（`web/api/auth_user.py` 里的 `_SESSION_MAX_AGE = 86400`），中间用得再频繁也不续期；而前端只在挂载时校验一次身份，`getMe()` 之后再没人问过服务器。两件事叠起来的症状很具体：**过一天回来，页面看着还在，实际已经死了，手动刷新才弹登录页**。

  现在窗口会滑动：带着有效 cookie 的请求，在窗口剩余不足一半时把 `expires_at` 往后推并重发 cookie。做成 ASGI 中间件而不是依赖，有两个理由——只有中间件还能在响应出门前加 header，而且一个没碰到鉴权路由的请求同样说明"人还在"。静态资源跳过：它们占了请求的大头，且不代表任何人在场。

  代价说清楚：带 cookie 的请求多一次主键 SELECT，UPDATE 和 `Set-Cookie` 只在窗口过半后发生。**续期不是无限的**——`KIMI_WEB_SESSION_ABSOLUTE_MAX_AGE`（默认 30 天，从登录时刻算，永不延长）是硬顶，一个被偷走的 cookie 到线就死，哪怕它每分钟都在用。

- **过期时界面自己回登录页**。此前 HTTP 401 只写一行 `console.error`，WebSocket 4401 报的是"reload the page to sign in again"——把重新加载这件事交给了读者去猜。现在两条路都汇到同一个信号：清掉缓存的用户，`App` 据此渲染登录页，工作区随之卸载，登录后重新挂载并重连 socket。标签页重新可见时还会重新校验一次（30 秒内不重复问）。

### Added

- **`KIMI_WEB_SESSION_MAX_AGE`**（空闲窗口，秒，默认 86400）和 **`KIMI_WEB_SESSION_ABSOLUTE_MAX_AGE`**（从登录起算的上限，默认 30 天）。桌面版在 `_build_env` 里默认给 30 天：本机 loopback、单人使用，24 小时那道门槛换不到多少安全，代价却是每天一次登录。这行设在用户 `.env` 之前，所以不同意的人可以自己覆盖。

## [v0.1.37] — 2026-08-24

### Fixed

- **桌面版的会话结束时不再什么都不留**。`archive_on_session_end` 只挂在命令行那条路的 `finally` 里，而桌面版走的是 worker，它的关闭处只有 `close()`。后果是：**一个没触发压缩的会话，既不写摘要也不产生提案**。真实库里的证据很干净——17 天里 9 条会话摘要，**触发方式全部是 compaction，session_end 一条都没有**。现在 worker 用和命令行相同的形状归档：失败不抛、限时 10 秒、在 `close()` 之前跑（soul 还持有历史）。代价说清楚：**每个 ≥4 条消息的会话结束时多一次摘要请求**，这笔钱命令行一直在付，现在两条路一样。

- **待批准的提案不再只在"相关时"被提起**。开场注入里那句 "raise one with the user when it is relevant" 是有意的默认（记忆系统不该骚扰你），但代价是话题撞不上的提案会一直躺到过期。这一版起，临近过期时措辞会变成"最老的还剩 N 天，本轮结束前提一次"。**这件事是测过的**：在一个完全不涉及队列主题的任务上，四轮各 30 次，原措辞的提出率是 **1%**（0–3%），升级后是 **25%**（20–40%）。25% 是改进，不是保证——所以界面那条路同样重要（见下）。

  子模块此前钉在这个改动之前的提交上，也就是说前一版装的 carryover 停在 1% 那一档。

### Added

- **Admin Panel 多了 Memory 页签，待批准条数直接显示在页签上**。此前记忆面板是**死代码**——自它被写出来那天起没有任何地方引用它，打包器把它摇掉了，所以 Suggested 页签、hook 和后端端点全都够不着。现在它在 admin 页面里，和 Skills、Knowledge 并排。三个端点走 `CandidateFile`：列出、保留、丢弃；**先写入后出队**（反过来会在写失败时静默丢掉提案，有测试盯着这个顺序）。

### Changed

- **搜索时发整句，不要发关键词**。工具描述此前写着 "a distinctive fragment works better than a sentence"，而 carryover 自己的召回基准说的正相反：整句 63.0%，最有辨识度的单个词 28.4%。扫描本身按逆文档频率加权，常见词早就不计分，没有噪音可剥。


## [v0.1.36] — 2026-08-23

### Added

- **待批准的记忆现在能在界面里处理**。carryover 的闸门是"抽取只提议，人来批准"，这套东西每一块都在——开场注入里列着待批准项连同 id 和 promote 操作，Memory 工具十二个动词也含 promote/dismiss——**唯独缺了人动手的那个入口**。开场注入的措辞是"相关时再向用户提起"，这是对的默认（记忆系统不该骚扰你），代价是话题撞不上的提案就一直躺着，两周后静默过期。发现时正好有 5 条躺了两天，讲的是一个那两天没被提起的项目，而它们的主人**没有任何办法看见它们**：Web API 对已入库条目有增删改查，对队列一个端点都没有。现在记忆面板多了 `Suggested` 页签，**条数角标在面板层计算**，所以你开着别的页签时那个数字也在——一个没人告诉你它存在的队列，问题从来不是不能处理。

  两处语义是照工具路径来的：**先写入、后出队**（反过来会在写失败时静默丢掉提案，有测试盯着这个顺序）；**这条路径不再加确认**，因为按钮就是决定，闸门要挡的是"没有决定就进了记忆"，不是让主人对自己的数据点两次。

### Fixed

- **跨平台 CI，以及它当场抓到的三个问题**。此前所有 job 都跑在 ubuntu，而 OpenKimo 是发 Windows 版的——`src/` 下二十三处 `win32` 分支从没被执行过。加上矩阵后：`msvcrt.locking` 按**进程**加锁，同进程第二个线程去锁同一段字节会被直接拒绝而不是等待（`fcntl.flock` 会排队），`skill/manager.py` 与 `wiki/workspaces.py` 两处阻塞锁都带着这个缺陷；`test_export_help_is_leaf_command` 断言的是带 ANSI 的帮助文本，CI 设了 `FORCE_COLOR` 就断在转义序列上，而输出对人眼完全一样；`tests/acp` 在 Windows 上不是失败而是**停住**——29 分半只跑完两个测试。Windows 那一档设为报告不阻断，首次诚实结果是 91 failed / 3608 passed，那个数字就是欠账。

- **构建会检查发出去的和源码一不一致**。此前两次打包故障都是无声的：pip 见版本号相同就跳过安装，发出的 `.app` 里是旧代码；venvstacks 的层只装不卸，改名后 amem 和 carryover 一起进了包，死的那个还能 import。现在签名前逐字节比对每个 workspace 包，反向用 `direct_url.json` 认出"本地装过但这次不再构建"的包。

- **测试不再写进开发者自己的目录**。`get_share_dir()` 回落到 `~/.kimi` 并且 mkdir，278 个测试文件里只有 17 个重定向了它，跑一遍套件会动到真实目录。


## [v0.1.35] — 2026-08-23

### Changed

- **记忆包改名 Carryover**。`Amem` 与 `A-MEM: Agentic Memory for LLM Agents` 撞名——NeurIPS 2025 的论文，两个实现各约一千星，同样两个音节、同一个领域。而这个包的 README 恰恰是一份跟其他记忆系统的对比，被读成那篇论文的实现两个方向都吃亏，副标题救不回一个读者已经认成别的东西的名字。`import carryover`、`python -m carryover check`，没有旧名兼容层：没发过 PyPI、没打过 tag，此刻没人在 import 它，为一个只存在一周的问题留一个永久答案不划算。

  改名时顺带修掉一处**不只是措辞**的地方：抽取提示词写着 `project — a durable fact about a repository`，这句话在告诉模型什么算数，于是一段没有仓库的对话等于关掉了四个 kind 之一。现在是领域中立的说法。**已公布的对比数字是旧措辞下测的。**

### Removed

- **删掉 Wiki 的每轮自动检索**。`retrieve on demand instead of on every prompt` 那次已经拿掉了调用点，实现留了下来；此后它的每个公开符号在模块外都是 0 引用，只被自己的测试供养着，还有另一条测试专门守着这个调用点不许回来。跑不了的代码不会因此就不用读——每个打开它的人都得先判断一次它是死是活。同去的还有它的专属测试、六个驱动它的可观测性测试、两个只服务于它的 UTF-8 边界测试，以及三个没有别处发出的遥测事件名。

### Fixed

- **vis 的会话列表看不见任何新会话**。会话搬进工作目录的 `session-data/` 之后，列表和统计接口仍然只扫 `share_dir/sessions/`——**那之后写入的每一个会话在界面上都不存在**。页面照常渲染，只是历史变短了，这比报错更难发现。两处现在走同一个枚举器，新旧位置都读，正是 `legacy_sessions_dir` 那句"retained for reads"的本意。统计接口此前**一个测试都没有**，这就是它能一直错着的原因。

- **测试套件会破坏运行它的那个仓库**。四个测试模块用 `cwd=` 临时目录调 git，以为这就决定了操作哪个仓库——不是，`GIT_DIR` 和 `GIT_WORK_TREE` 优先级更高，而 git 会把它们导出给自己执行的每一个钩子。从 pre-push 钩子里跑套件时，临时目录里的 `git init` 重新初始化了**真实仓库**（`core.bare=true`，此后那个检出里任何 git 命令都不工作），同一个 fixture 的 `git config user.email t@t.com` 落进了开发者的配置，**下一个提交就会用这个身份**。三十九个测试的失败是它们自己造成的破坏的副作用。

- **四十四个失败的测试**。这个套件没人跑：CI 跑的是外层仓库的测试，而 kimi-cli 自己的 workflow 不是这里的门禁。除上面两条外，其余是测试落后于代码——假 session 对象缺 `work_dir`、mock 缺 `max_request_bytes`、进度条助手把 rich 画的底槽也算成已填充、e2e 的 mock 服务器把会话结束的记忆归档请求当成 tool-call 续写而报 500 导致重试被计成对话轮次。

- **构建规范不再入库**。`packaging/venvstacks.resolved.toml` 由 `build.py` 每次构建生成，带着生成它那台机器的绝对路径。它不只是躺着——两处 release 缓存键都在对它求哈希，于是决定构建要不要复用缓存的是一份过期产物，而且依赖真的变了它也不会变。缓存键改为 `venvstacks.toml` 和 `kimi-cli/uv.lock`。

### Added

- **检查跑在会真正执行的地方**。CI 在这个账号上起不了任何 job（每次运行三秒内因计费失败），所以 `.githooks` 下装了 pre-commit（ruff，不到一秒）和 pre-push（不含 e2e 的 3773 个测试，约两分半）。两者都可以 `--no-verify` 跳过，这是有意的：一个跳不过去的钩子会被卸载掉。CI 的配置同时接上了 kimi-cli 的套件，计费恢复即生效。


## [v0.1.34] — 2026-08-20

### Added

- **跨会话记忆：agent 现在记得住上次说过的事，而且记什么由你点头**。此前持久记忆里只有 agent 当场想起来要记的东西——也就是说，你纠正它一次、约定一个路径、定下一条规矩，它记不记全看它那一刻有没有想到。现在每次会话结束抽取一次（跑在本来就要生成摘要的地方，所以是一次调用而不是每轮开销），把值得带进以后对话的事实**排进队列**，等你批准才入库。`suggest()` 入队、`keep()` 提交是两个分开的调用，好让这个区别没法被一个开关抹掉；你直接说出来的事实走 `add()`，因为没什么可批准的。抽取提示词有一处是拿零和五的差距换来的：**正文必须嵌在提示词里，不能追加在后面**——放在最后，模型会把自己的最后一轮当成待续的那轮然后接着写下去，六个真实会话实测**每次产出的可用提案都是零**；把正文用标签围起来、任务说明放在它之后，同样六个会话每个产出四到五条。

- **记忆分两类，到达方式不同**。`user`/`feedback` 是**行为类**——它们改变 agent 怎么工作，而且从来不会成为提问的对象（"对外文档里不许出现函数名"是靠**不做某件事**来遵守的，没人会输入一个能检索到它的查询），所以每次对话**全文注入**，不管当下用不用得上。`project`/`reference` 是**查阅类**，只在索引里占一行，需要时按需取全文——把它们全都随身带着，比偶尔去取一次正确答案更贵。在一位用户的真实工作记录上测了 243 条发言：**89% 是指令**，那些场景里根本没有检索发生，能帮上忙的只有不请自来的那半。

- **记忆能被检索，而且中文真的能搜到**。SQLite FTS5 的默认分词器按非字母数字切分，而中文不带空格——一整句会变成一个 token，任何针对句中短语的查询都会落空。改用 `trigram` 分词器加一条按**逆文档频率**加权的子串扫描：英文有停用词表挡住"是什么"这类语法词，中文没有，而为每种语言维护一张表是打不赢的仗；改成看每个词在**这个库里**能把范围缩小多少，语法很常见于是不再计分，主题很罕见于是决定顺序。仅这一处，中文召回@8 从 47.7% 到 58.9%。两条路按查询的文字系统逐条选择，所以中文这条提升而英文那条一点不受损。两万条时一次中文查询 9.8ms、写一条再搜索 50ms（此前 66ms / 756ms）。

- **记忆会告诉你它满了、旧了、重复了——但一件都不自己动手**。行为类记忆总共约八千字符，超过之后最老的就不再到场，而**一条不再到场的长期指令，就是一条不再被遵守的指令**，并且没有任何迹象表明它消失了。所以有三个只提建议的机制：**取代**（新记录替换旧的，靠序列相似度、主题词重合，以及"改用…不再维护…"这类**自我宣告**——宣告比前两个分数都更有力）、**休眠**（主题几个月没出现的条目；注意它**不**测量"这条规则有没有被用过"，因为规则是靠不做某件事来遵守的，按使用打分会最先淘汰禁令）、**压力**（预算占用到 75% 就提，超过 80% 改成"本轮结束前必须提一次"——温和的措辞实测在一个 83% 的真实库上放了几个星期，清单每次会话都在，一次都没被提起）。答复有两种：`retire` 或 `affirm`，**后者更常是对的**——后来的指令通常是在旧的之上补充而不是替代，它没写到的部分仍然是要求；一个真实的库上，第三代规则丢掉了前两代带着的三项要求，照建议退役会把它们无声抹掉。要合并就用 `consolidate`，它先写合并条、后退役其余的。

- **事实自带日期**。写"2026-05-06 用户把估计方式改成了 EWMA"，而不是"用 EWMA"。条目本身有 `created_at`，但那回答的是另一个问题——它**被写下来**的时间，不是它**成立**的时间。十二个会话、六条事实中途被修订的测量里，让每个回答都是当前值的不是去重（一次都没触发，按设计也不该触发），而是两个版本都留在库里、各自带着日期。

### Changed

- **技能默认全部关闭，直到你打开它**。每一个被发现的技能都在**每次请求**上花掉几百字符的上下文，不管它是否被用到；而发现会走到你可能都不记得有的目录——另一个 agent 的目录、一个项目检出、某个装了技能的插件。此前状态记的是"哪些被关掉了"，于是**"开着"成了所有没人回答过的问题的答案**，包括昨天才出现的技能。现在状态记的是哪些开着。三种情形，中间那种是这不是一行改动的原因：全新安装没有状态文件，什么都不带；**已有状态文件没有 `enabled` 键的，按旧规则读——升级不是清空别人整理好的目录的时刻**，否则第一个迹象会是 agent 突然不会做它上周做过的事；状态文件损坏按全新安装处理，因为一个读不出来的文件不能算作"有人选择了带上全部技能"。另外，管理规则拒绝的名字（`写作助手/`、前导符号、空格）此前被答为"开着"，反过来之后会变成"永远关着且打不开"——那是一次没人执行过的删除，所以开关和检查现在都退回发现本身用的规范化，面板因此能寻址一个它本来管不了的技能。

- **记忆实现搬到 Carryover，本仓库只是普通调用方**。[Carryover](https://github.com/goldenxingxing/carryover) 0.1.0 作为 workspace 成员引入，2891 行里约 2800 行的重复副本删除。留在这里的是真正属于本应用的部分：kosong 消息转文本、compaction 与关闭钩子、文件位置、批准与展示。这么做是因为副本已经付过一次代价——手工搬运的 `fold_text` 丢了三个归一化轴里的两个，而它同时撑着 Wiki 的意图哈希（一个授权边界），在写出测试之前没有任何东西发现。现在两边共用同一个实现。顺带拿到 Carryover 侧的修复：**SQLite 低于 3.34 时英文搜索此前静默返回空**（FTS5 的 trigram 分词器要 3.34+，而失败被读成"没匹配上"），现在退回扫描，结果与正常环境一致。

### Fixed

- **打包时装的是旧代码**。`pip` 看到版本号一样就跳过安装，而 workspace 成员每次本地构建版本号都不变——于是源码改了、wheel 是对的、`.app` 里还是旧的，而且完全无声。这次是一个新增常量抛了 `ImportError` 才暴露；**任何不抛异常的改动——改个阈值、改个提示词、修个 bug——都会安安静静地没进包**。构建前删掉本地 wheel 的 dist-info，让 pip 当它没装过。

- **`.app` 里装了两份 kimi_cli**，DMG 因此多出 134MB。移除影子副本时必须保留 dist-info，否则 `importlib.metadata.version` 找不到发行版，server 起不来——这一条是靠实际启动 server 打 `/healthz` 才发现的，测试套接不住。

- **打包的 workspace 成员名单不再手抄一份**。加一个成员要在两个仓库改两处，而构建只会在解析阶段报"PyPI 上找不到这个包"，看起来像网络问题。现在从 `pyproject.toml` 的 workspace 声明读，wheel 的安装顺序也从同一处推导——手抄的顺序里漏掉一个成员，它会排到最后、在它的依赖方之后安装。


## [v0.1.33] — 2026-08-17

### Added

- **后台任务干完了会主动告诉你，不用再等你开口**。一个在前台会话结束之后才完成的任务，此前没有任何办法把结果说出来：通知发布了、`llm` sink 一直 pending，直到你恰好因为别的事情再发一条消息，它才被读到。所以"后台还在跑"的会话永远不会主动回来给结论。交互式终端一直是有这个能力的——任务完成会唤醒它的循环、重新进 soul 让模型读通知并汇报；而 wire server（所有 Web 和桌面客户端）根本没有这个循环，只有客户端发 `prompt` 才跑一轮。现在两边用**同一个信号**（`background_tasks.completion_event`）行为一致。护栏照搬终端踩过的坑：**首轮之后才武装**（否则重开一个有历史通知的会话，它会自己开口讲旧事）、**只在空闲时触发**（正在跑的那轮本来就会读到）、**合并 1.5 秒**（五个 subagent 前后脚结束是一次汇报而不是五轮）、**连续失败三次就停**（不在 provider 挂掉时每次完成都重试）。默认开启，与终端一致，`background.auto_followup` 可关——代价要说清楚：它会在你没开口的情况下花 token。唤醒的那一轮输入是 system-reminder，界面不再为"输入完全是 reminder"的轮次画用户气泡，那不是你说的话。
- **审批和提问里你手输的内容，会作为你的消息出现在对话历史里**。拒绝审批时填的理由、AskUserQuestion 里手打的回答，此前只经由工具结果抵达模型，对话读起来就是 agent 自问自答，你说的那句话在记录里不存在。现在由 soul 发一条 `UserReply` 事件承载——之所以是 soul 发而不是前端自己画，是因为只有前者会被写进 wire 文件并被回放；响应帧本身不落盘，刷新一次就没了。只回显**手输**的内容：选一个列出的选项是"选择"而不是"你说的话"，判定规则是答案不在该问题的选项列表里。
- **Skill 分类可折叠**。十几个分类几百个 skill，全展开意味着要滑过一切才能够到任何东西。分区默认折叠，标题成为一张带数量的目录，点开一个是一次点击而不是一段滚动；展开状态跨会话记住（你常用的分类不会天天变），并提供 Expand/Collapse all。存储不可读、不可写或不存在时一律按"全部折叠"处理，不会因此让页面渲染失败。

### Fixed

- **拒绝一次顺手的记录，不再把整个任务一起中断**。root agent 只要收到一个不带反馈的拒绝就会结束当前 turn。对"agent 被要求做的事"这是对的——否决 `rm -rf` 意味着方案错了，继续下去就是撇开用户往前走。但 memory 和 wiki 是 agent 在干别的事情时**自己顺手**发起的副动作，拒绝它的含义是"别存这条"，被当成了"停下"，于是你真正要的任务被丢在半路，而你对那件事什么都没说过。`ToolRejectedError` 增加 `stops_turn`（默认 True，其余所有工具行为不变），turn 结束的判断只统计声明会中断的那些；memory 与 wiki 显式退出，并换掉提示语——默认那句是"停下来等用户告诉你怎么做"，正好和该发生的事相反。
- **Plan 模式的说明不再因为鼠标路过就弹出来**。共享 Tooltip 的悬停延迟是 0，而 Plan 恰好位于 YOLO 开关和会话列表之间——从 YOLO 回到会话列表必然扫过它，扫过即弹。是新增的 YOLO 开关让 Plan 第一次变成"路过必经之地"，所以症状是从打开 YOLO 之后开始的。两个开关现在都要求有意悬停（500ms）。无头浏览器实测：快速路过不再弹，停 800ms 仍然弹。

## [v0.1.32] — 2026-08-13

### Fixed

- **Windows 上"打开会话就 lost connection"，其实是两件事**。第一件：一台机器上跑起了两个实例，后启动的那个永远抢不到端口，守护进程每 2 秒重启一次 uvicorn，一份日志里转了 **947 次、一个半小时**，界面上只表现为会话掉线。撑住这个循环的是三个各自独立的缺陷。**健康检查从来没有在检查任何东西**——它探的是 `/health`，而这个路由根本不存在：SPA 兜底路由对任何 GET 都返回 index.html 和 200，于是任何端口都是"健康"的，包括被别人占着的那个；刚死于 10048 的子进程因此被判为 RUNNING，重启计数器被清零，`MAX_AUTO_RESTARTS = 3` 形同虚设。现在探 `/healthz`，要求 JSON 且 `status == "ok"`，并把响应里的 `pid` 和自己 spawn 的子进程比对——一个 HTML 页面配 200 不是健康检查。**没有任何东西清理残留监听者**：`_reap_orphan_listener` 调的是 `lsof` 和 `ps`，Windows 上两个命令都不存在，整个函数被异常吞掉，于是托盘被强杀后遗留的 uvicorn 能一直占着端口；Windows 分支改用 `netstat -ano` 定位、PowerShell 读命令行，只杀命令行包含 `kimi_cli.web.app` 的进程，绝不按端口盲杀。**没有任何东西阻止开第二个实例**：现在用命名内核 mutex（进程被强杀时系统自动释放句柄，pidfile 做不到这点），第二次启动会把已有实例的页面打开再退出，而不是静默消失——一个点了没反应的图标本身就是故障。端口确实被占用时（不管占用者是不是我们），守护进程不再 spawn，托盘直接显示 `Port 5494 in use — close the other copy`，而不是让用户去看一份他打不开的日志。顺带把 `uvicorn-log.json` 改成原子写：两个实例并发读写同一个文件，会让其中一个死在解析自己的日志配置上（日志里那两次 `JSONDecodeError`）。
- **配置了 Session Token 之后，登录了也连不上会话**。第二件事：HTTP 和 WebSocket 的鉴权条件不一致。`AuthMiddleware` 在设置了 session token 时接受**两种**凭据——session token，**或**有效的 `kimi_session` 登录 cookie（这个 fallback 是特意写的）；而 WebSocket 端点只认 `?token=`。于是"在设置里填了 Session Token + 用账号密码登录"这个组合会精确地落进：**每个 HTTP 请求 200，每个 WebSocket 握手被拒**（服务端日志里是 403）。界面无从表达这件事——close code 被丢掉，5 次重试花在一个重试不可能改变的拒绝上，最后报成 "lost connection to the session and could not reconnect"，读起来像网络故障，把人引向完全错误的方向。现在凭据规则由 `user_from_connection` 共用：它接受 `HTTPConnection`，而一次握手和一个请求一样都是；两条路径走同一条规则。客户端也不再丢弃原因：4401/4403 归入终态 close code，各自给出服务端真正说的那句话（"未登录"／"服务器拒绝了这个连接"），而不是赖在连接上。
- **切换会话时，聊天记录有时停在最顶部**。两层原因。滚动状态活过了它所描述的会话：只有内层虚拟列表带 key，组件本身和它的 catch-up 标记不会随切换重建，于是在任何一个会话里往上滚过一次，下一个会话就继承了"不要跟随"，最终停在哪里取决于历史消息恰好在哪个时刻到达。更根本的一层是：`followOutput` 滚到最后一条时，刚加进去的条目还在用 `defaultItemHeight`（160px）估算高度，真实消息远比这高，等它们被测量出来内容底部就往下跑了，而条目数已不再变化，`followOutput` 不会被第二次问到——**估算错多少，就停在离底部多远的地方**（实测差 21,006px）。现在 catch-up 期间同时跟随总高度，滚动到某个 index 本身不改变高度所以会收敛；读者第一次滚轮/触摸/按键即解除，位置仍然归他自己。

### Added

- **审核弹窗顶部给出一行提炼**。原来那段内容本身就是 `JSON.stringify(display_block)`——一次编辑会把前后全文当 JSON 摊在屏幕上，而这是知道自己在批准什么*之后*才需要读的东西。现在最上面一行直接说清楚：`执行命令 · git push origin main`、`修改文件 · src/a.ts · 3 → 5 行 · 共 2 个文件`。它取自结构化的 display block 而不是散文，所以给的是命令和路径本身，而不是把动作词重复一遍；原始 JSON 收进折叠区。
- **Plan 右侧新增 YOLO 开关**，悬停有说明，等同于 `/yolo` 命令。刻意做成"设置为某个值"而不是"翻转"——终端里的 `/yolo` 和第二个客户端都会动这个状态，翻转只会把客户端最后看到的值取反；`/yolo` 现在也广播状态，所以在哪一边切换，另一边都会跟着动。`initialize` 会汇报当前值，因为 `--yolo` 可以在任何 wire 消息之前就打开，而它不像 plan mode 那样能靠 replay 重建。开启时标签变琥珀色：自动放行是唯一一个代价完全静默的开关。

## [v0.1.31] — 2026-08-10

### Added

- **安装包自带一个便携 shell，Windows 不装 Git 也能执行命令**：Shell 工具走的是 bash 而不是 PowerShell，所以一台没装 Git for Windows 的机器等于**一个 shell 都没有**。安装包现在附带一份钉死版本、校验过 sha256 的 busybox-w32（`FRP-6075`，x86_64 取 **w64u** 这个 Unicode 变体——不带 Unicode 的构建会把非 ASCII 路径和输出弄乱，对中文用户不是可选项），找不到 git-bash 时自动退到它。二进制不进仓库（`deps/bin` 是 ignore 的），由构建脚本按校验和拉取；拉不到也只是告警，不会让整个安装包构建失败。它被安装成 `sh.exe` 而非 `busybox.exe` 是刻意的：shell 的调用形式是 `<path> -c <command>`，而 busybox 按 argv[0] 分发，仍叫 busybox.exe 会去找一个名为 `-c` 的 applet。用户侧还有两条逃生口：`KIMI_CLI_SHELL_PATH` 可以指向任意 POSIX shell（全平台有效），或者把便携 shell 放到 `%USERPROFILE%\.kimi\bin\sh.exe`——后者不需要重装就能给已有安装补上。注意提示词里报告的 shell 名按文件名判定，busybox 的 ash 会如实报成 `sh` 而不是 `bash`，避免模型写出它跑不了的 bashism。
- **Skill 使用统计**：管理面板新增用量视图——按天的调用曲线、Top skills、会话数与显式 `/skill:` 调用占比，以及每张卡片上的近 14 天迷你柱状图和最近一次使用时间。数据由会话 wire log 重建，不新增任何埋点；扫描有时间预算，触顶时会标注 "Partial" 而不是悄悄给一个偏小的数。已经不在安装列表里、但历史上被调用过的 skill 单独归入 "Used but not installed"，方便发现改名和误删。
- **Skill 按分类分组，并可整类启停/删除**：386 个内置 skill 平铺成一张卡片墙，找东西只能靠肉眼。排序新增 "Category"，每个分类一个带标题和数量的分区，分区内仍按调用次数排。分类的来源是"能声明就声明，声明不了才猜"：SKILL.md 的 frontmatter 里写 `category:`（顶层或 `metadata` 下都认）具有最高优先级，取值可以是分类 id、完整标签或常见别名（`education` → Learning & Career，`ops`/`infra` → DevOps 等），写了库里没有的值也不会被丢掉或塞进 Other，而是自成一个分区；没声明的才按名字、其次描述做关键词判定。分类标题栏上还有整类的 Enable / Disable / Delete，按钮上直接标出会影响多少个，删除有二次确认。批量操作走的是一次性的服务端接口而不是循环调单个接口——一整类四十来个 skill 的状态在一次写入里落盘，不会出现中间态可见、或者失败一半停在半途。整类启用**不会**复活已删除的 skill：被删的内置 skill 不等于"被禁用"，从分类按钮里悄悄恢复太意外，恢复仍走卡片上的 Restore。

### Fixed

- **Windows 上找不到 bash 不再让整个产品打不开**：探测阶段一旦定位不到 `bash.exe` 就抛 `GitBashNotFoundError`，而这个调用发生在建 Runtime 的路上，于是"Shell 工具用不了"被放大成"会话根本起不来"——聊天、读写文件、Web UI、以及绝大多数不碰命令行的 skill 全都被一起拖下水。现在探测不再抛异常，缺少 shell 只是一条数据（连同一段可操作的修复说明一起带着），由真正需要 shell 的那一个工具在被调用时报告；前台和后台两条执行路径都有闸，不会拿一个字符串化的 `None` 去起进程。副作用是 Web 端那条"请安装 Git for Windows"的提示以前只看 git-bash 在不在，一台靠便携 shell 正常跑命令的机器会被一直误报，现在它看的是"到底有没有可用的 shell"。

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
