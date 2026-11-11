# Providers and Models

OpenKimo supports multiple LLM platforms, which can be configured via configuration files.

## Platform selection

The easiest way to configure is to manually edit the configuration file (see [Configuration files](./config-files.md)) or use the `/model` command to switch models.

All built-in platforms have been removed; `providers` and `models` are configured entirely from the configuration file.

## Provider types

The `type` field in `providers` configuration specifies the API provider type. Different types use different API protocols and client implementations.

| Type | Description |
| --- | --- |
| `openai_legacy` | OpenAI Chat Completions API |
| `openai_responses` | OpenAI Responses API |
| `anthropic` | Anthropic Claude API |
| `gemini` | Google Gemini API |
| `vertexai` | Google Vertex AI |

All provider types support adding custom HTTP headers via the `custom_headers` field. See [Configuration files](./config-files.md) for details.

### `openai_legacy`

For platforms compatible with OpenAI Chat Completions API, including the official OpenAI API and various compatible services.

```toml
[providers.openai]
type = "openai_legacy"
base_url = "https://api.openai.com/v1"
api_key = "sk-xxx"
```

### `openai_responses`

For OpenAI Responses API (newer API format).

```toml
[providers.openai-responses]
type = "openai_responses"
base_url = "https://api.openai.com/v1"
api_key = "sk-xxx"
```

### `anthropic`

For connecting to Anthropic Claude API.

```toml
[providers.anthropic]
type = "anthropic"
base_url = "https://api.anthropic.com"
api_key = "sk-ant-xxx"
```

### `gemini`

For connecting to Google Gemini API.

```toml
[providers.gemini]
type = "gemini"
base_url = "https://generativelanguage.googleapis.com"
api_key = "xxx"
```

### `vertexai`

For connecting to Google Vertex AI. Requires setting necessary environment variables via the `env` field.

```toml
[providers.vertexai]
type = "vertexai"
base_url = "https://xxx-aiplatform.googleapis.com"
api_key = ""
env = { GOOGLE_CLOUD_PROJECT = "your-project-id" }
```

## Model capabilities

The `capabilities` field in model configuration declares the capabilities supported by the model. This affects feature availability in Kimo Code CLI.

| Capability | Description |
| --- | --- |
| `thinking` | Supports thinking mode (deep reasoning), can be toggled |
| `always_thinking` | Always uses thinking mode (cannot be disabled) |
| `image_in` | Supports image input |
| `video_in` | Supports video input |

```toml
[models.gemini-3-pro-preview]
provider = "gemini"
model = "gemini-3-pro-preview"
max_context_size = 262144
capabilities = ["thinking", "image_in"]
```

### `thinking`

Declares that the model supports thinking mode. When enabled, the model performs deeper reasoning before answering, suitable for complex problems. In shell mode, you can use the `/model` command to switch models and thinking mode, or control it at startup with `--thinking` / `--no-thinking` flags.

### `always_thinking`

Indicates the model always uses thinking mode and cannot be disabled. For example, models with "thinking" in their name typically have this capability. When using such models, the `/model` command won't prompt for thinking mode toggle.

### `image_in`

When image input capability is enabled, you can paste images in conversations (`Ctrl-V`).

### `video_in`

When video input capability is enabled, you can send video content in conversations.

## Fetch tool

The `FetchURL` tool fetches web page content using local HTTP requests (with SSRF protection and Playwright fallback for 403 responses), then extracts the main text with `trafilatura`.
