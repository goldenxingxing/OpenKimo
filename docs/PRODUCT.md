# OpenKimo 产品介绍

> 一个浏览器标签页，驱动会写代码、能跑任务的 AI Agent。

OpenKimo 是开箱即用的 **AI Agent 平台**。与只能对话的 AI 聊天工具不同，OpenKimo 的 Agent 拥有完整的执行能力：读写文件、运行 Shell 命令、搜索与抓取网页、执行 Python 分析、管理后台长任务、调度子 Agent 协作。你用自然语言描述目标，Agent 自己拆解步骤、调用工具、交付结果。

- **桌面一键安装**：macOS DMG / Windows 安装器，零依赖，拖拽即用
- **服务器一条命令**：`docker compose up -d` 部署完整多用户平台
- **多模型支持**：Kimi（Moonshot）、OpenAI 兼容、Anthropic，随时切换
- **默认安全**：Docker 版每个会话强制运行在独立沙箱容器中

## 它能做什么


| 场景    | 典型任务                      | 依托能力                    |
| ----- | ------------------------- | ----------------------- |
| 代码工程  | 定位并修复 Bug、补测试、重构、读懂陌生仓库   | Shell、文件读写、代码搜索         |
| 数据分析  | 清洗数据、统计分析、图表与 PDF/Word 报告 | Jupyter/Python、可视化与文档技能 |
| 网页调研  | 多源搜索、页面抓取、竞品对比、结构化结论      | 网页搜索/抓取、无头 Chromium     |
| 文档写作  | 论文、报告、周报、演示材料的成品文件        | 写作技能、docx/pdf 生成        |
| 批量自动化 | 文件批处理、格式转换、图片处理           | 后台任务、子 Agent 并行         |
| 团队平台  | 共享 Agent 基础设施、账号隔离、品牌定制   | 多用户 RBAC、沙箱、资源限额        |


内置数百个技能（skills）按需自动调用；支持安装自定义技能包、接入 MCP 工具、用 YAML 定义专属 Agent（限定工具集与系统提示词）。

## 三种使用形态

### 桌面应用（个人推荐）

macOS（Apple Silicon / Intel）与 Windows 10/11 x64。安装后常驻菜单栏/托盘，首次启动在 Settings 窗口填入 LLM API Key，浏览器自动打开 `http://127.0.0.1:5494` 即可开始会话。

### Docker 部署（团队推荐）

```bash
curl -O https://raw.githubusercontent.com/goldenxingxing/OpenKimo-releases/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/goldenxingxing/OpenKimo-releases/main/.env.example
cp .env.example .env   # 填入至少一个 LLM API Key
docker compose up -d
```

宿主机仅需 Docker Engine。双容器架构：**Gateway** 负责 Web 服务与容器编排，每个会话动态克隆独立的 **Sandbox** 容器（内含 Agent Worker、Jupyter 内核、无头 Chromium），并施加 CPU / 内存 / 磁盘 / 进程数限额——Agent 满载运行也不影响宿主机。默认账号 `admin` / `admin123`，首次登录后请立即在 `/admin` 修改。

### 本地模式（开发调试）

无 Docker 直接在宿主机运行（`scripts/start.sh --mode=local`），会禁用会话沙箱、浏览器与 Jupyter，仅推荐开发场景。

## 使用方式

1. **按项目建会话**：每个会话绑定一个工作目录，Agent 的文件操作都发生在该目录内。
2. **直接说目标**：「找出仓库里未被测试覆盖的公共函数并补上测试」优于手把手拆步骤；大任务 Agent 会自行规划并并行调用工具。
3. **长任务转后台**：耗时任务自动进入后台执行，完成时通知；主会话随时可继续对话。
4. **随时换模型**：Settings 中可配置多个 Provider（各自的 Key / Base URL / 模型 / 上下文长度），指定默认并随时切换。

## 主要配置


| 配置项                     | 说明                                                   |
| ----------------------- | ---------------------------------------------------- |
| LLM Provider            | 多 Provider 并存（kimi / openai_legacy / anthropic），指定默认 |
| API Key / Base URL / 模型 | 每个 Provider 独立，支持自定义网关与最大上下文                         |
| Thinking / Temperature  | 思考过程展示、生成温度                                          |
| Web 端口                  | 默认 `5494`                                            |
| Session Token           | Web 界面访问令牌，局域网共享时建议开启                                |
| 沙箱限额（Docker 版）          | 每会话 CPU（默认 2 核）、内存（默认 4G）等                           |


## 适用人群

- **独立开发者**：几分钟内拥有一个能实际动手的 AI 工程助手
- **小团队**：一台服务器共享 Agent 基础设施，无需在每台机器装运行时
- **安全敏感场景**：代码永不在宿主机执行，会话间完全隔离
- **OEM / 私有化团队**：品牌名、Logo、页面标题可从管理面板或构建参数定制，白标开箱即用

## 下载与资源


| 资源            | 地址                                                                                                                           |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| 安装包（GitHub）   | [https://github.com/goldenxingxing/OpenKimo-releases/releases](https://github.com/goldenxingxing/OpenKimo-releases/releases) |
| 安装包（Gitee 镜像） | [https://gitee.com/qunwei/OpenKimo-releases/releases](https://gitee.com/qunwei/OpenKimo-releases/releases)                   |
| Docker 镜像     | `ghcr.io/goldenxingxing/kimi-agent-gateway` · `ghcr.io/goldenxingxing/kimi-agent-sandbox`                                    |


**系统要求**：macOS 14+（Apple Silicon / Intel）；Windows 10/11 x64；Linux 需 Docker Engine。