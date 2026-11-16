# Model Context Protocol

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 是一个开放协议，让 AI 模型可以安全地与外部工具和数据源交互。Kimo Code CLI 支持连接 MCP 服务器，扩展 AI 的能力。

## MCP 是什么

MCP 服务器提供「工具」给 AI 使用。例如，一个数据库 MCP 服务器可以提供查询工具，让 AI 能够执行 SQL 查询；一个浏览器 MCP 服务器可以让 AI 控制浏览器进行自动化操作。

Kimo Code CLI 内置了一些工具（文件读写、Shell 命令、网页抓取等），通过 MCP 你可以添加更多工具，比如：

- 访问特定 API 或数据库
- 控制浏览器或其他应用
- 与第三方服务集成（GitHub、Linear、Notion 等）

## MCP 服务器管理

使用 [`kimo mcp`](../reference/kimo-mcp.md) 命令管理 MCP 服务器，也可以用 Web 管理面板的 **MCP** 页签，两者读写同一份 `mcp.json`（见[在管理面板里管理](#在管理面板里管理)）。

**添加服务器**

添加 HTTP 服务器：

```sh
# 基本用法
kimo mcp add --transport http context7 https://mcp.context7.com/mcp

# 带 Header
kimo mcp add --transport http context7 https://mcp.context7.com/mcp \
  --header "CONTEXT7_API_KEY: your-key"

# 使用 OAuth 认证
kimo mcp add --transport http --auth oauth linear https://mcp.linear.app/mcp
```

添加 stdio 服务器（本地进程）：

```sh
kimo mcp add --transport stdio chrome-devtools -- npx chrome-devtools-mcp@latest
```

**列出服务器**

```sh
kimo mcp list
```

在 Kimo Code CLI 运行时，也可以输入 `/mcp` 查看已连接的服务器和加载的工具。

**移除服务器**

```sh
kimo mcp remove context7
```

**OAuth 授权**

对于使用 OAuth 的服务器，需要先完成授权：

```sh
kimo mcp auth linear
```

这会打开浏览器完成 OAuth 流程。授权成功后，Kimo Code CLI 会保存 token 供后续使用。

MCP OAuth token 存储在 `~/.kimo/mcp-oauth/`。从使用 FastMCP 2.x 的旧版本升级后，旧的 token 缓存不会自动迁移；如果 `kimo mcp list` 显示某个 OAuth 服务器需要授权，重新运行 `kimo mcp auth <name>` 即可。

**测试服务器**

```sh
kimo mcp test context7
```

## MCP 配置文件

MCP 服务器配置存储在 `~/.kimo/mcp.json`，格式与其他 MCP 客户端兼容：

```json
{
  "mcpServers": {
    "context7": {
      "url": "https://mcp.context7.com/mcp",
      "headers": {
        "CONTEXT7_API_KEY": "your-key"
      }
    },
    "chrome-devtools": {
      "command": "npx",
      "args": ["chrome-devtools-mcp@latest"],
      "env": {
        "SOME_VAR": "value"
      }
    }
  }
}
```

**临时加载配置**

使用 `--mcp-config-file` 参数可以加载其他位置的配置文件：

```sh
kimo --mcp-config-file /path/to/mcp.json
```

使用 `--mcp-config` 参数可以直接传入 JSON 配置：

```sh
kimo --mcp-config '{"mcpServers": {"test": {"url": "https://..."}}}'
```

## 在管理面板里管理

Web 管理面板的 **MCP** 页签读写的就是 `~/.kimo/mcp.json`，和 CLI 同一份文件。它能列出已配置的服务器，增删改，
测试某个服务器的连接并列出它提供的工具；表单覆盖不到的字段，可以直接编辑原始 JSON。

已保存的凭据——header 的值、`env` 的值、以及作为 bearer token 的 `auth`——显示为 `***尾部` 的掩码形式。
原样提交掩码值表示"保持不变"，输入新值则覆盖。原始 JSON 编辑器是例外：它显示文件本来的内容，包括凭据。

服务器是在会话 worker 启动时连接的，所以保存改动会重启空闲会话。正在生成回答的会话保留旧的服务器集合，
并会在提示里被点名，下次重启时才生效。

**stdio 服务器与多人部署。** stdio 配置项是一条由宿主机执行的命令，所以从浏览器配置它等于让管理员账号执行任意代码。
桌面版允许这么做；其他部署形态默认拒绝，需要显式设置 `KIMO_WEB_ALLOW_STDIO_MCP=1`；开启了
`KIMO_WEB_RESTRICT_SENSITIVE_APIS` 的部署会像对待 `config.toml` 一样，直接拒绝全部 MCP 接口。
在宿主机上跑 `kimo mcp add` 不受这些限制影响。

## 加载状态

MCP 服务器在 Shell UI 启动后异步初始化，不会阻塞界面的使用。Shell 底部状态栏会实时显示连接进度，
连接完成后自动切换为就绪状态。Web 界面在会话连接服务器期间会显示一条占位消息；如果某个服务器连接失败或需要授权，
这条占位消息会变成一行说明并点名该服务器——它的工具在这个会话接下来的时间里都是缺失的。

如果配置了多个 MCP 服务器，加载时间可能较长，状态栏的进度指示可以帮助你了解当前连接情况。

## 安全性

MCP 工具可能会访问和操作外部系统，需要注意安全风险。

**审批机制**

Kimo Code CLI 对敏感操作（如文件修改、命令执行）会请求用户确认。MCP 工具也遵循同样的审批机制，所有 MCP 工具调用都会弹出确认提示。

**提示词注入风险**

MCP 工具返回的内容可能包含恶意指令，试图诱导 AI 执行危险操作。Kimo Code CLI 会对工具返回内容进行标记，帮助 AI 区分工具输出和用户指令，但你仍应：

- 只使用可信来源的 MCP 服务器
- 检查 AI 提议的操作是否合理
- 对于高风险操作保持手动审批

::: warning 注意
在 YOLO 或 AFK 模式下，MCP 工具调用也会被自动批准。仅在完全信任 MCP 服务器时使用这些模式。
:::
