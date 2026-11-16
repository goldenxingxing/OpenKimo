# Getting Started

## What is Kimo Code CLI

Kimo Code CLI is an AI agent that runs in the terminal, helping you complete software development tasks and terminal operations. It can read and edit code, execute shell commands, search and fetch web pages, and autonomously plan and adjust actions during execution.

Kimo Code CLI is suited for:

- **Writing and modifying code**: Implementing new features, fixing bugs, refactoring code
- **Understanding projects**: Exploring unfamiliar codebases, answering architecture and implementation questions
- **Automating tasks**: Batch processing files, running builds and tests, executing scripts

Kimo Code CLI supports the following usage modes:

- **[Interactive CLI (`kimo`)](../reference/kimo-command.md)**: Chat with AI in the terminal using natural language or execute shell commands directly
- **[Browser UI (`kimo web`)](../reference/kimo-web.md)**: Open a graphical interface in your local browser, with session management, file references, code highlighting, and more
- **[Agent integration (`kimo acp`)](../reference/kimo-acp.md)**: Run as a service and integrate with [IDEs](./ides.md) and other local agent clients via the [Agent Client Protocol]

::: info Tip
If you encounter issues or have suggestions, please provide feedback on [GitHub Issues](https://github.com/goldenxingxing/OpenKimo/issues).
:::

[Agent Client Protocol]: https://agentclientprotocol.com/

## Installation

Run the installation script to complete the installation. The script will first install [uv](https://docs.astral.sh/uv/) (a Python package manager), then install Kimo Code CLI via uv:

```sh
# Linux / macOS
curl -LsSf https://code.kimo.com/install.sh | bash
```

```powershell
# Windows (PowerShell)
Invoke-RestMethod https://code.kimo.com/install.ps1 | Invoke-Expression
```

Verify the installation:

```sh
kimo --version
```

::: tip
Due to macOS security checks, the first run of the `kimo` command may take longer. You can add your terminal application in "System Settings → Privacy & Security → Developer Tools" to speed up subsequent launches.
:::

If you already have uv installed, you can also run:

```sh
uv tool install --python 3.13 kimo-cli
```

::: tip
Kimo Code CLI supports Python 3.12–3.14, with Python 3.13 recommended.
:::

## Upgrade and uninstall

Upgrade to the latest version:

```sh
uv tool upgrade kimo-cli --no-cache
```

Uninstall Kimo Code CLI:

```sh
uv tool uninstall kimo-cli
```

## First run

Run the `kimo` command in the project directory where you want to work to start Kimo Code CLI:

```sh
cd your-project
kimo
```

On first launch, you need to configure your API source. All built-in platforms have been removed; edit the configuration file to add a provider and model. See [Providers](../configuration/providers.md) for details.

Now you can chat with Kimo Code CLI directly using natural language. Try describing a task you want to complete, for example:

```
Show me the directory structure of this project
```

::: tip
If the project doesn't have an `AGENTS.md` file, you can run the `/init` command to have Kimo Code CLI analyze the project and generate this file, helping the AI better understand the project structure and conventions.
:::

Enter `/help` to view all available [slash commands](../reference/slash-commands.md) and usage tips.
