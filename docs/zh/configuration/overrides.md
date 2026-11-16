# 配置覆盖

Kimo Code CLI 的配置可以通过多种方式设置，不同来源的配置按优先级覆盖。

## 优先级

配置的优先级从高到低为：

1. **环境变量** - 最高优先级，用于临时覆盖或 CI/CD 环境
2. **CLI 参数** - 启动时指定的参数
3. **配置文件** - `~/.kimo/config.toml` 或通过 `--config-file` 指定的文件

## CLI 参数

### 配置文件相关

| 参数 | 说明 |
| --- | --- |
| `--config <TOML/JSON>` | 直接传入配置内容，覆盖默认配置文件 |
| `--config-file <PATH>` | 指定配置文件路径，替代默认的 `~/.kimo/config.toml` |

`--config` 和 `--config-file` 不能同时使用。

### 模型相关

| 参数 | 说明 |
| --- | --- |
| `--model, -m <NAME>` | 指定使用的模型名称 |

`--model` 指定的模型必须在配置文件的 `models` 中定义。如果未指定，使用配置文件中的 `default_model`。

### 行为相关

| 参数 | 说明 |
| --- | --- |
| `--thinking` | 启用 thinking 模式 |
| `--no-thinking` | 禁用 thinking 模式 |
| `--yolo, --yes, -y` | 自动批准所有工具调用（用户仍可回应 `AskUserQuestion`） |
| `--afk` | Away-from-keyboard：自动批准所有工具调用，并自动 dismiss `AskUserQuestion` |
| `--plan` | 以计划模式启动 |

`--thinking` / `--no-thinking` 会覆盖上次会话保存的 thinking 状态。如果不指定，使用上次会话的状态。

`--plan` 对新会话启用计划模式；恢复已有会话时强制开启计划模式。也可以在配置文件中设置 `default_plan_mode = true` 让新会话默认进入计划模式。

## 环境变量覆盖

环境变量可以在不修改配置文件的情况下覆盖供应商和模型设置。这在以下场景特别有用：

- CI/CD 环境中注入密钥
- 临时测试不同的 API 端点
- 在多个环境间切换

环境变量根据当前使用的供应商类型来决定是否生效：

- `kimo` 类型的供应商：使用 `KIMO_*` 环境变量
- `openai_legacy` 或 `openai_responses` 类型的供应商：使用 `OPENAI_*` 环境变量
- 其他类型的供应商：不支持环境变量覆盖

完整的环境变量列表请参阅 [环境变量](./env-vars.md)。

示例：

```sh
OPENAI_API_KEY="sk-xxx" kimo
```

## 配置优先级示例

假设配置文件 `~/.kimo/config.toml` 内容如下：

```toml
default_model = "gpt-4o"

[providers.openai]
type = "openai_legacy"
base_url = "https://api.openai.com/v1"
api_key = "sk-config"

[models.gpt-4o]
provider = "openai"
model = "gpt-4o"
max_context_size = 128000
```

以下是不同场景的配置来源：

| 场景 | `base_url` | `api_key` | `model` |
| --- | --- | --- | --- |
| `openai_legacy` | 配置文件 | 配置文件 | 配置文件 |
| `OPENAI_API_KEY=sk-env kimo` | 配置文件 | 环境变量 | 配置文件 |
| `kimo --model other` | 配置文件 | 配置文件 | CLI 参数 |
