# `kimo term` Subcommand

The `kimo term` command launches the [Toad](https://github.com/batrachianai/toad) terminal UI, a modern terminal interface built with [Textual](https://textual.textualize.io/).

```sh
kimo term [OPTIONS]
```

## Description

[Toad](https://github.com/batrachianai/toad) is a graphical terminal interface for Kimo Code CLI that communicates with the Kimo Code CLI backend via the ACP protocol. It provides a richer interactive experience with better output rendering and layout.

When you run `kimo term`, it automatically starts a `kimo acp` server in the background, and Toad connects to it as an ACP client.

## Options

All extra options are passed through to the internal `kimo acp` command. For example:

```sh
kimo term --work-dir /path/to/project --model gpt-4o
```

Common options:

| Option | Description |
|--------|-------------|
| `--work-dir PATH` | Specify working directory |
| `--model NAME` | Specify model |
| `--yolo` | Auto-approve all tool calls |

For the full list of options, see [`kimo` command](./kimo-command.md).

## System requirements

::: warning Note
`kimo term` requires Python 3.14+. If you installed Kimo Code CLI with an older Python version, you need to reinstall with Python 3.14:

```sh
uv tool install --python 3.14 kimo-cli
```
:::
