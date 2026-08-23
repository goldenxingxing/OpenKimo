import json
import subprocess
from pathlib import Path


def test_no_skill_ships_inside_the_package() -> None:
    """Skills are installed by the user, not bundled.

    This asserted the opposite until the 300 bundled skills were removed:
    that six named ones existed and that the count was exactly 300. Shipping
    them cost 274 MB, and every name stayed listed after the files went, so
    the admin panel showed rows that answered "Skill not found".

    Kept as a test rather than left to the build because the failure mode is
    silent — a skill added under src/ works locally and only shows up as
    package size.
    """
    root = Path(__file__).parents[1]
    builtin = root / "kimi-cli/src/kimi_cli/skills"

    assert not (root / "skills").exists()
    assert tuple(builtin.glob("*/SKILL.md")) == ()
    tracked = subprocess.check_output(
        ["git", "-C", root / "kimi-cli", "ls-files", "src/kimi_cli/skills"],
        text=True,
    ).splitlines()

    assert tracked == []


def test_packaged_skills_do_not_include_local_secrets_or_caches() -> None:
    root = Path(__file__).parents[1]
    builtin = root / "kimi-cli/src/kimi_cli/skills"
    tracked = subprocess.check_output(
        ["git", "-C", root / "kimi-cli", "ls-files", "src/kimi_cli/skills"],
        text=True,
    ).splitlines()

    assert not tuple(path for path in tracked if path.endswith("/.env"))
    assert not tuple(path for path in tracked if path.endswith("/.DS_Store"))
    assert not tuple(path for path in tracked if "/__pycache__/" in path)
    assert not tuple(path for path in tracked if path.endswith(".pyc"))
    assert not tuple(path for path in tracked if "/node_modules/" in path)
    assert not tuple(path for path in tracked if "/obj/" in path)


def test_the_builtin_skill_directory_is_still_collected_when_it_has_content() -> None:
    """The packaging rule survives the directory being empty.

    There is no bundled xlsx skill to check a launcher on any more — that test
    went with the skills. What is worth keeping is the rule that puts a skill
    into the build at all, so that installing one into the package again does
    not silently fail to ship.
    """
    root = Path(__file__).parents[1]
    source = (root / "kimi-cli/src/kimi_cli/utils/pyinstaller.py").read_text(encoding="utf-8")

    assert '"skills/**"' in source


def test_pyinstaller_collects_builtin_skills() -> None:
    root = Path(__file__).parents[1]
    source = (root / "kimi-cli/src/kimi_cli/utils/pyinstaller.py").read_text()

    assert '"skills/**"' in source


def test_python_and_pyinstaller_packages_include_wiki_templates() -> None:
    root = Path(__file__).parents[1]
    pyproject = (root / "kimi-cli/pyproject.toml").read_text(encoding="utf-8")

    assert 'include = ["src/kimi_cli/wiki/templates/**"]' in pyproject

    inspection = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-c",
            (
                "import json; from pathlib import Path; import kimi_cli; "
                "from kimi_cli.utils.pyinstaller import datas; "
                "root = Path(kimi_cli.__file__).parent; "
                "print(json.dumps([str(Path(source).relative_to(root)) "
                "for source, _ in datas if Path(source).is_relative_to(root)]))"
            ),
        ],
        cwd=root / "kimi-cli",
        check=True,
        capture_output=True,
        text=True,
    )
    staged = set(json.loads(inspection.stdout))
    assert {
        "wiki/templates/schema.md",
        "wiki/templates/index.md",
        "wiki/templates/overview.md",
        "wiki/templates/log.md",
        "wiki/templates/manifest.json",
    } <= staged
