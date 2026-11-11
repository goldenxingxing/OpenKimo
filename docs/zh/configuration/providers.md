# 平台与模型

OpenKimo 支持多种 LLM 平台，可以通过配置文件进行配置。

## 平台选择

最简单的配置方式是手动编辑配置文件（见 [配置文件](./config-files.md)）或使用 `/model` 命令切换模型。

所有内置平台已移除；`providers` 与 `models` 完全通过配置文件配置。

## 供应商类型

`providers` 配置中的 `type` 字段指定 API 供应商类型。不同类型使用不同的 API 协议和客户端实现。

| 类型 | 说明 |
| --- | --- |
| `openai_legacy` | OpenAI Chat Completions API |
| `openai_responses` | OpenAI Responses API |
| `anthropic` | Anthropic Claude API |
| `gemini` | Google Gemini API |
| `vertexai` | Google Vertex AI |

所有供应商类型都支持通过 `custom_headers` 字段添加自定义 HTTP 请求头。详见 [配置文件](./config-files.md)。

### `openai_legacy`

兼容 OpenAI Chat Completions API 的平台，包括 OpenAI 官方 API 和各种兼容服务。

```toml
[providers.openai]
type = "openai_legacy"
base_url = "https://api.openai.com/v1"
api_key = "sk-xxx"
```

### `openai_responses`

用于 OpenAI Responses API（较新的 API 格式）。

```toml
[providers.openai-responses]
type = "openai_responses"
base_url = "https://api.openai.com/v1"
api_key = "sk-xxx"
```

### `anthropic`

用于连接 Anthropic Claude API。

```toml
[providers.anthropic]
type = "anthropic"
base_url = "https://api.anthropic.com"
api_key = "sk-ant-xxx"
```

### `gemini`

用于连接 Google Gemini API。

```toml
[providers.gemini]
type = "gemini"
base_url = "https://generativelanguage.googleapis.com"
api_key = "xxx"
```

### `vertexai`

用于连接 Google Vertex AI。需要通过 `env` 字段设置必要的环境变量。

```toml
[providers.vertexai]
type = "vertexai"
base_url = "https://xxx-aiplatform.googleapis.com"
api_key = ""
env = { GOOGLE_CLOUD_PROJECT = "your-project-id" }
```

## 模型能力

模型配置中的 `capabilities` 字段声明模型支持的能力。这会影响 Kimo Code CLI 的功能可用性。

| 能力 | 说明 |
| --- | --- |
| `thinking` | 支持 Thinking 模式（深度思考），可开关 |
| `always_thinking` | 始终使用 Thinking 模式（不可关闭） |
| `image_in` | 支持图片输入 |
| `video_in` | 支持视频输入 |

```toml
[models.gemini-3-pro-preview]
provider = "gemini"
model = "gemini-3-pro-preview"
max_context_size = 262144
capabilities = ["thinking", "image_in"]
```

### `thinking`

声明模型支持 Thinking 模式。启用后，模型会在回答前进行更深入的推理，适合复杂问题。在 Shell 模式下，可以通过 `/model` 命令切换模型和 Thinking 模式，或在启动时通过 `--thinking` / `--no-thinking` 参数控制。

### `always_thinking`

表示模型始终使用 Thinking 模式，无法关闭。例如名称中包含 "thinking" 的模型通常具有此能力。使用这类模型时，`/model` 命令不会提示选择 Thinking 模式的开关。

### `image_in`

启用图片输入能力后，可以在对话中粘贴图片（`Ctrl-V`）。

### `video_in`

启用视频输入能力后，可以在对话中发送视频内容。

## 抓取工具

`FetchURL` 工具使用本地 HTTP 请求（含 SSRF 防护与 403 响应的 Playwright 回退）抓取网页内容，再用 `trafilatura` 提取正文。

