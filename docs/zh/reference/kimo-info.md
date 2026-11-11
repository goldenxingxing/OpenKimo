# `kimo info` 子命令

`kimo info` 显示 Kimo Code CLI 的版本和协议信息。

```sh
kimo info [--json]
```

## 选项

| 选项 | 说明 |
|------|------|
| `--json` | 以 JSON 格式输出 |

## 输出内容

| 字段 | 说明 |
|------|------|
| `kimo_cli_version` | Kimo Code CLI 版本号 |
| `agent_spec_versions` | 支持的 Agent 规格版本列表 |
| `wire_protocol_version` | Wire 协议版本 |
| `python_version` | Python 运行时版本 |

## 示例

**文本输出**

```sh
$ kimo info
kimo-cli version: 1.20.0
agent spec versions: 1
wire protocol: 1.10
python version: 3.13.1
```

**JSON 输出**

```sh
$ kimo info --json
{"kimo_cli_version": "1.20.0", "agent_spec_versions": ["1"], "wire_protocol_version": "1.10", "python_version": "3.13.1"}
```
