# 常见问题

## 安装与鉴权

### API 密钥无效

API 密钥无效可能的原因：

- **密钥输入错误**：检查是否有多余的空格或遗漏的字符。
- **密钥已过期或被撤销**：在平台控制台确认密钥状态。
- **环境变量覆盖**：检查是否有 `OPENAI_API_KEY` 环境变量覆盖了配置文件中的密钥。可以运行 `echo $OPENAI_API_KEY` 检查。

### 会员过期或配额用尽

可以通过 `/usage` 命令查看当前的配额和使用状态。

## 交互问题

### Shell 模式中 `cd` 命令无效

在 Shell 模式中执行 `cd` 命令不会改变 Kimo Code CLI 的工作目录。这是因为每次 Shell 命令在独立的子进程中执行，目录切换只在该进程内生效。

如果需要切换工作目录：

- **退出并重新启动**：在目标目录中重新运行 `kimo` 命令。
- **使用 `--work-dir` 参数**：启动时指定工作目录，如 `kimo --work-dir /path/to/project`。
- **在命令中使用绝对路径**：直接使用绝对路径执行命令，如 `ls /path/to/dir`。

### 粘贴图片失败

使用 `Ctrl-V` 粘贴图片时，如果提示 "Current model does not support image input"，说明当前模型不支持图片输入。

解决方法：

- **切换到支持图片的模型**：使用支持 `image_in` 能力的模型。
- **检查剪贴板内容**：确保剪贴板中确实有图片数据，而非图片文件的路径。

### 工作目录被删除或移除

如果在会话期间工作目录变得不可访问（外置硬盘拔出、目录被删除或文件系统卸载），Kimo Code CLI 会检测到这一情况并显示崩溃报告，包含会话 ID 和工作目录路径，之后干净退出。你可以通过 `kimo -r <session-id>` 在正确的目录中恢复会话。

## ACP 问题

### IDE 无法连接到 Kimo Code CLI

如果 IDE（如 Zed 或 JetBrains IDE）无法连接到 Kimo Code CLI，请检查以下几点：

- **确认 Kimo Code CLI 已安装**：运行 `kimo --version` 确认安装成功。
- **检查配置路径**：确保 IDE 配置中的 Kimo Code CLI 路径正确。通常可以使用 `kimo acp` 作为命令。
- **检查 uv 路径**：如果使用 uv 安装，确保 `~/.local/bin` 在 PATH 中。可以使用绝对路径，如 `/Users/yourname/.local/bin/kimo acp`。
- **查看日志**：检查 `~/.kimo/logs/kimo.log` 中的错误信息。

## MCP 问题

### MCP 服务启动失败

添加 MCP 服务器后，如果工具未加载或报错，可能是以下原因：

- **命令不存在**：对于 stdio 类型的服务器，确保命令（如 `npx`）在 PATH 中。可以使用绝对路径配置。
- **配置格式错误**：检查 `~/.kimo/mcp.json` 是否为有效的 JSON 格式。运行 `kimo mcp list` 查看当前配置。

调试步骤：

```sh
# 查看已配置的服务器
kimo mcp list

# 测试服务器是否正常
kimo mcp test <server-name>
```

### OAuth 授权失败

对于需要 OAuth 授权的 MCP 服务器（如 Linear），如果授权失败：

- **检查网络连接**：确保能访问授权服务器。
- **重新授权**：运行 `kimo mcp auth <server-name>` 重新进行授权。
- **重置授权**：如果授权信息损坏，可以运行 `kimo mcp reset-auth <server-name>` 清除后重试。

### Header 格式错误

添加 HTTP 类型的 MCP 服务器时，Header 格式应为 `KEY: VALUE`（冒号后有空格）。例如：

```sh
# 正确
kimo mcp add --transport http context7 https://mcp.context7.com/mcp --header "CONTEXT7_API_KEY: your-key"

# 错误（缺少空格或使用等号）
kimo mcp add --transport http context7 https://mcp.context7.com/mcp --header "CONTEXT7_API_KEY=your-key"
```

## Print/Wire 模式问题

### JSONL 输入格式无效

使用 `--input-format stream-json` 时，输入必须是有效的 JSONL（每行一个 JSON 对象）。常见问题：

- **JSON 格式错误**：确保每行是完整的 JSON 对象，没有语法错误。
- **编码问题**：确保输入使用 UTF-8 编码。
- **换行符问题**：Windows 用户注意检查换行符是否为 `\n` 而非 `\r\n`。

正确的输入格式示例：

```json
{"role": "user", "content": "你好"}
```

### Print 模式无输出

如果 `--print` 模式下没有输出，可能是：

- **未提供输入**：需要通过 `--prompt`（或 `--command`）或 stdin 提供输入。例如：`kimo --print --prompt "你好"`。
- **输出被缓冲**：尝试使用 `--output-format stream-json` 获取流式输出。
- **配置未完成**：确保已配置 API 密钥和模型（见 [平台与模型](./configuration/providers.md)）。

## 更新与升级

### macOS 首次运行缓慢

macOS 的 Gatekeeper 安全机制会在首次运行新程序时进行检查，导致启动变慢。解决方法：

- **等待检查完成**：首次运行时耐心等待，后续启动会恢复正常。
- **添加到开发者工具**：在「系统设置 → 隐私与安全性 → 开发者工具」中添加你的终端应用。

### 如何升级 Kimo Code CLI

使用 uv 升级到最新版本：

```sh
uv tool upgrade kimo-cli --no-cache
```

添加 `--no-cache` 参数可以确保获取最新版本。

### 启动时弹出更新提醒

当后台检查发现新版本时，Kimo Code CLI 会在 Shell 启动前显示一个阻断式更新提醒，列出当前版本和最新版本信息。你可以通过以下按键选择操作：

- `Enter`：立即升级到最新版本
- `q`：暂时跳过，下次启动时继续提醒
- `s`：跳过该版本，不再提醒（直到有更新的版本发布）

### 如何禁用更新提醒

如果不希望 Kimo Code CLI 检查更新或在启动时显示更新提醒，可以设置环境变量：

```sh
export KIMO_CLI_NO_AUTO_UPDATE=1
```

这会同时禁用后台更新检查、启动时的阻断式更新提醒和欢迎面板中的版本提示。可以将此行添加到你的 shell 配置文件（如 `~/.zshrc` 或 `~/.bashrc`）中。
