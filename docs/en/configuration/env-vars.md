# Environment Variables

OpenKimo supports overriding configuration or controlling runtime behavior through environment variables. This page lists all supported environment variables.

For detailed information on how environment variables override configuration files, see [Config Overrides](./overrides.md).

## Model environment variables

The following environment variables override provider and model configuration.

| Environment Variable | Description |
| --- | --- |
| `KIMO_MODEL_MAX_CONTEXT_SIZE` | Maximum context length (in tokens) |
| `KIMO_MODEL_CAPABILITIES` | Model capabilities, comma-separated (e.g., `thinking,image_in`) |
| `KIMO_MODEL_TEMPERATURE` | Generation parameter `temperature` |
| `KIMO_MODEL_TOP_P` | Generation parameter `top_p` |
| `KIMO_MODEL_MAX_COMPLETION_TOKENS` | Explicit hard cap for `max_completion_tokens` |
| `KIMO_MODEL_MAX_TOKENS` | Compatibility alias for `KIMO_MODEL_MAX_COMPLETION_TOKENS` |

### `KIMO_MODEL_MAX_CONTEXT_SIZE`

Overrides the model's `max_context_size` field in the configuration file. Must be a positive integer.

```sh
export KIMO_MODEL_MAX_CONTEXT_SIZE="262144"
```

### `KIMO_MODEL_CAPABILITIES`

Overrides the model's `capabilities` field in the configuration file. Multiple capabilities are comma-separated, supported values are `thinking`, `always_thinking`, `image_in`, and `video_in`.

```sh
export KIMO_MODEL_CAPABILITIES="thinking,image_in"
```

### `KIMO_MODEL_TEMPERATURE`

Sets the generation parameter `temperature`, controlling output randomness. Higher values produce more random output, lower values produce more deterministic output.

```sh
export KIMO_MODEL_TEMPERATURE="0.7"
```

### `KIMO_MODEL_TOP_P`

Sets the generation parameter `top_p` (nucleus sampling), controlling output diversity.

```sh
export KIMO_MODEL_TOP_P="0.9"
```

### `KIMO_MODEL_MAX_COMPLETION_TOKENS`

Sets an explicit hard cap for the generation parameter `max_completion_tokens`. When unset,
OpenKimo uses the model's remaining context window. The value is dynamically clamped to
`max_context_size - input_tokens` for every request.

```sh
export KIMO_MODEL_MAX_COMPLETION_TOKENS="4096"
```

`KIMO_MODEL_MAX_TOKENS` is still accepted. If both variables are set,
`KIMO_MODEL_MAX_COMPLETION_TOKENS` takes precedence. Set either variable to `0` or a negative
integer to disable completion-token clamping.

## OpenAI-compatible environment variables

The following environment variables take effect when using `openai_legacy` or `openai_responses` type providers.

| Environment Variable | Description |
| --- | --- |
| `OPENAI_BASE_URL` | API base URL |
| `OPENAI_API_KEY` | API key |

### `OPENAI_BASE_URL`

Overrides the provider's `base_url` field in the configuration file.

```sh
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

### `OPENAI_API_KEY`

Overrides the provider's `api_key` field in the configuration file.

```sh
export OPENAI_API_KEY="sk-xxx"
```

## Other environment variables

| Environment Variable | Description |
| --- | --- |
| `KIMO_SHARE_DIR` | Customize the share directory path (default: `~/.kimo`) |
| `KIMO_CLI_NO_AUTO_UPDATE` | Disable all update-related features |
| `KIMO_CLI_PASTE_CHAR_THRESHOLD` | Character threshold for folding pasted text (default: `1000`) |
| `KIMO_CLI_PASTE_LINE_THRESHOLD` | Line threshold for folding pasted text (default: `15`) |

### `KIMO_SHARE_DIR`

Customize the share directory path for OpenKimo. The default path is `~/.kimo`, where configuration, sessions, logs, and other runtime data are stored.

```sh
export KIMO_SHARE_DIR="/path/to/custom/kimo"
```

See [Data Locations](./data-locations.md) for details.

::: warning Note
`KIMO_SHARE_DIR` does not affect [Agent Skills](../customization/skills.md) search paths. Skills are cross-tool shared capability extensions (compatible with Claude, Codex, etc.), which is a different type of data from application runtime data. To override Skills paths, use the `--skills-dir` flag.
:::

### `KIMO_CLI_NO_AUTO_UPDATE`

When set to `1`, `true`, `t`, `yes`, or `y` (case-insensitive), disables all update-related features, including background auto-update check, the blocking update gate on startup, and the version hint in the welcome panel.

```sh
export KIMO_CLI_NO_AUTO_UPDATE="1"
```

::: tip
If you installed OpenKimo via Nix or other package managers, this environment variable is typically set automatically since updates are handled by the package manager.
:::

### `KIMO_CLI_PASTE_CHAR_THRESHOLD`

In Agent mode, when pasted text exceeds this character count, it is folded into a placeholder (e.g., `[Pasted text #1 +10 lines]`) and expanded to full content on submit. Default: `1000`.

```sh
export KIMO_CLI_PASTE_CHAR_THRESHOLD="1000"
```

### `KIMO_CLI_PASTE_LINE_THRESHOLD`

In Agent mode, when pasted text reaches this line count, it is folded into a placeholder. Default: `15`.

```sh
export KIMO_CLI_PASTE_LINE_THRESHOLD="15"
```

::: tip
Some terminals (e.g., XShell over SSH) may break CJK input methods (Chinese/Japanese/Korean IME) after pasting multiline text. Symptoms include the IME candidate window not appearing or input becoming unresponsive until Ctrl+C is pressed.

This happens because multiline text in the input buffer can confuse the terminal's cursor position tracking, which affects IME composition window placement. You can work around this by lowering the line threshold to fold multiline pastes into single-line placeholders:

```sh
export KIMO_CLI_PASTE_LINE_THRESHOLD="2"
```

With this setting, any paste containing a newline will be automatically folded, preventing multiline text from entering the input buffer. Single-line pastes (URLs, short commands, etc.) are not affected.

Note: The two thresholds use OR logic (character count **or** line count), so lowering only the line threshold is sufficient. Avoid setting the character threshold to a very small value (e.g., `1`), as that would fold all non-empty pastes including single-line short text.
:::
