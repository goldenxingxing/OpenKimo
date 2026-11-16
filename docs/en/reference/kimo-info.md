# `kimo info` Subcommand

`kimo info` displays version and protocol information for Kimo Code CLI.

```sh
kimo info [--json]
```

## Options

| Option | Description |
|--------|-------------|
| `--json` | Output in JSON format |

## Output

| Field | Description |
|-------|-------------|
| `kimo_cli_version` | Kimo Code CLI version number |
| `agent_spec_versions` | List of supported agent spec versions |
| `wire_protocol_version` | Wire protocol version |
| `python_version` | Python runtime version |

## Examples

**Text output**

```sh
$ kimo info
kimo-cli version: 1.20.0
agent spec versions: 1
wire protocol: 1.10
python version: 3.13.1
```

**JSON output**

```sh
$ kimo info --json
{"kimo_cli_version": "1.20.0", "agent_spec_versions": ["1"], "wire_protocol_version": "1.10", "python_version": "3.13.1"}
```
