# Integrations with Tools

Besides using in the terminal and IDEs, Kimo Code CLI can also be integrated with other tools.

## Zsh plugin

[zsh-kimo-cli](https://github.com/MoonshotAI/zsh-kimo-cli) is a Zsh plugin that lets you quickly switch to Kimo Code CLI in Zsh.

**Installation**

If you use Oh My Zsh, you can install it like this:

```sh
git clone https://github.com/MoonshotAI/zsh-kimo-cli.git \
  ${ZSH_CUSTOM:-~/.oh-my-zsh/custom}/plugins/kimo-cli
```

Then add the plugin in `~/.zshrc`:

```sh
plugins=(... kimo-cli)
```

Reload the Zsh configuration:

```sh
source ~/.zshrc
```

**Usage**

After installation, press `Ctrl-X` in Zsh to quickly switch to Kimo Code CLI without manually typing the `kimo` command.

::: tip
If you use other Zsh plugin managers (like zinit, zplug, etc.), please refer to the [zsh-kimo-cli repository](https://github.com/MoonshotAI/zsh-kimo-cli) README for installation instructions.
:::
