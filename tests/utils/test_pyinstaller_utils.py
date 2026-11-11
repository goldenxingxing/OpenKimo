from __future__ import annotations

import platform
import sys
from pathlib import Path

from inline_snapshot import snapshot


def test_pyinstaller_datas():
    from kimo_cli.utils.pyinstaller import datas

    project_root = Path(__file__).parent.parent.parent
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    rg_binary = "rg.exe" if platform.system() == "Windows" else "rg"

    def shown(path: Path) -> str:
        """Project-relative where that means anything, absolute otherwise.

        The environment is usually `.venv` beside the project, but it does not
        have to be: a run against another Python version, or CI pointing
        UV_PROJECT_ENVIRONMENT elsewhere, puts site-packages outside the tree
        entirely. Assuming `.venv` made this the one test that failed there,
        and assuming the environment is *under* the root made it raise instead.
        """
        try:
            return path.relative_to(project_root).as_posix()
        except ValueError:
            return path.as_posix()

    venv_dir = shown(Path(sys.prefix))
    site_packages = f"{venv_dir}/lib/python{python_version}/site-packages"
    datas = [
        (
            shown(Path(path)).replace(f"{venv_dir}/Lib/site-packages", site_packages),
            Path(dst).as_posix(),
        )
        for path, dst in datas
    ]

    datas = [(p, d) for p, d in datas if "web/static" not in d and "vis/static" not in d]

    expected_datas = [
        (
            f"{site_packages}/dateparser/data/dateparser_tz_cache.pkl",
            "dateparser/data",
        ),
        (
            f"{site_packages}/fastmcp/../fastmcp-3.2.4.dist-info/INSTALLER",
            "fastmcp/../fastmcp-3.2.4.dist-info",
        ),
        (
            f"{site_packages}/fastmcp/../fastmcp-3.2.4.dist-info/METADATA",
            "fastmcp/../fastmcp-3.2.4.dist-info",
        ),
        (
            f"{site_packages}/fastmcp/../fastmcp-3.2.4.dist-info/RECORD",
            "fastmcp/../fastmcp-3.2.4.dist-info",
        ),
        (
            f"{site_packages}/fastmcp/../fastmcp-3.2.4.dist-info/REQUESTED",
            "fastmcp/../fastmcp-3.2.4.dist-info",
        ),
        (
            f"{site_packages}/fastmcp/../fastmcp-3.2.4.dist-info/WHEEL",
            "fastmcp/../fastmcp-3.2.4.dist-info",
        ),
        (
            f"{site_packages}/fastmcp/../fastmcp-3.2.4.dist-info/entry_points.txt",
            "fastmcp/../fastmcp-3.2.4.dist-info",
        ),
        (
            f"{site_packages}/fastmcp/../fastmcp-3.2.4.dist-info/licenses/LICENSE",
            "fastmcp/../fastmcp-3.2.4.dist-info/licenses",
        ),
        (
            "src/kimo_cli/CHANGELOG.md",
            "kimo_cli",
        ),
        ("src/kimo_cli/agents/default/agent.yaml", "kimo_cli/agents/default"),
        ("src/kimo_cli/agents/default/coder.yaml", "kimo_cli/agents/default"),
        ("src/kimo_cli/agents/default/explore.yaml", "kimo_cli/agents/default"),
        ("src/kimo_cli/agents/default/plan.yaml", "kimo_cli/agents/default"),
        ("src/kimo_cli/agents/default/system.md", "kimo_cli/agents/default"),
        ("src/kimo_cli/agents/okabe/agent.yaml", "kimo_cli/agents/okabe"),
        ("src/kimo_cli/prompts/compact.md", "kimo_cli/prompts"),
        ("src/kimo_cli/prompts/init.md", "kimo_cli/prompts"),
        ("src/kimo_cli/tools/agent/description.md", "kimo_cli/tools/agent"),
        ("src/kimo_cli/tools/ask_user/description.md", "kimo_cli/tools/ask_user"),
        (
            "src/kimo_cli/tools/dmail/dmail.md",
            "kimo_cli/tools/dmail",
        ),
        ("src/kimo_cli/tools/background/list.md", "kimo_cli/tools/background"),
        ("src/kimo_cli/tools/background/output.md", "kimo_cli/tools/background"),
        ("src/kimo_cli/tools/background/stop.md", "kimo_cli/tools/background"),
        (
            "src/kimo_cli/tools/file/glob.md",
            "kimo_cli/tools/file",
        ),
        (
            "src/kimo_cli/tools/file/grep.md",
            "kimo_cli/tools/file",
        ),
        (
            "src/kimo_cli/tools/file/read.md",
            "kimo_cli/tools/file",
        ),
        (
            "src/kimo_cli/tools/file/read_media.md",
            "kimo_cli/tools/file",
        ),
        (
            "src/kimo_cli/tools/file/replace.md",
            "kimo_cli/tools/file",
        ),
        (
            "src/kimo_cli/tools/file/write.md",
            "kimo_cli/tools/file",
        ),
        ("src/kimo_cli/tools/memory/description.md", "kimo_cli/tools/memory"),
        ("src/kimo_cli/tools/plan/description.md", "kimo_cli/tools/plan"),
        ("src/kimo_cli/tools/plan/enter_description.md", "kimo_cli/tools/plan"),
        ("src/kimo_cli/tools/shell/bash.md", "kimo_cli/tools/shell"),
        (
            "src/kimo_cli/tools/think/think.md",
            "kimo_cli/tools/think",
        ),
        (
            "src/kimo_cli/tools/todo/set_todo_list.md",
            "kimo_cli/tools/todo",
        ),
        (
            "src/kimo_cli/tools/web/fetch.md",
            "kimo_cli/tools/web",
        ),
        ("src/kimo_cli/wiki/templates/index.md", "kimo_cli/wiki/templates"),
        ("src/kimo_cli/wiki/templates/log.md", "kimo_cli/wiki/templates"),
        ("src/kimo_cli/wiki/templates/manifest.json", "kimo_cli/wiki/templates"),
        ("src/kimo_cli/wiki/templates/overview.md", "kimo_cli/wiki/templates"),
        ("src/kimo_cli/wiki/templates/schema.md", "kimo_cli/wiki/templates"),
    ]
    # Bundled binaries are listed only when they are actually in the tree, the
    # same way the ripgrep one is: which of them a checkout has depends on how
    # it was set up, and a hard list turns that into a failing test.
    for binary in (rg_binary, "sh.exe"):
        if (project_root / "src/kimo_cli/deps/bin" / binary).exists():
            expected_datas.append((f"src/kimo_cli/deps/bin/{binary}", "kimo_cli/deps/bin"))

    assert sorted(datas) == sorted(expected_datas)


def test_pyinstaller_hiddenimports():
    from kimo_cli.utils.pyinstaller import hiddenimports

    assert sorted(hiddenimports) == snapshot(
        [
            "kimo_cli._build_info",
            "kimo_cli.cli.export",
            "kimo_cli.cli.info",
            "kimo_cli.cli.mcp",
            "kimo_cli.cli.plugin",
            "kimo_cli.cli.vis",
            "kimo_cli.cli.web",
            "kimo_cli.evolution.cli",
            "kimo_cli.tools",
            "kimo_cli.tools.agent",
            "kimo_cli.tools.ask_user",
            "kimo_cli.tools.background",
            "kimo_cli.tools.context_retrieve",
            "kimo_cli.tools.display",
            "kimo_cli.tools.dmail",
            "kimo_cli.tools.file",
            "kimo_cli.tools.file.glob",
            "kimo_cli.tools.file.grep_local",
            "kimo_cli.tools.file.managed_wiki",
            "kimo_cli.tools.file.plan_mode",
            "kimo_cli.tools.file.read",
            "kimo_cli.tools.file.read_media",
            "kimo_cli.tools.file.replace",
            "kimo_cli.tools.file.utils",
            "kimo_cli.tools.file.write",
            "kimo_cli.tools.memory",
            "kimo_cli.tools.plan",
            "kimo_cli.tools.plan.enter",
            "kimo_cli.tools.plan.heroes",
            "kimo_cli.tools.shell",
            "kimo_cli.tools.skill_install",
            "kimo_cli.tools.test",
            "kimo_cli.tools.think",
            "kimo_cli.tools.todo",
            "kimo_cli.tools.utils",
            "kimo_cli.tools.web",
            "kimo_cli.tools.web.fetch",
            "kimo_cli.tools.wiki",
            "setproctitle",
        ]
    )


def test_pyinstaller_hiddenimports_include_lazy_cli_subcommands():
    from kimo_cli.cli._lazy_group import LazySubcommandGroup
    from kimo_cli.utils.pyinstaller import hiddenimports

    expected_hiddenimports = {
        module_name
        for module_name, _attribute_name, _help_text in LazySubcommandGroup.lazy_subcommands.values()
    }

    assert expected_hiddenimports <= set(hiddenimports)
