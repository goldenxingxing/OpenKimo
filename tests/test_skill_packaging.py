import json
import subprocess
from pathlib import Path


def test_all_packaged_skills_have_one_source_location() -> None:
    root = Path(__file__).parents[1]
    builtin = root / "kimi-cli/src/kimi_cli/skills"
    skill_files = tuple(builtin.glob("*/SKILL.md"))

    assert not (root / "skills").exists()
    assert (builtin / "kimi-cli-help/SKILL.md").is_file()
    assert (builtin / "skill-creator/SKILL.md").is_file()
    assert (builtin / "grounding-dino-seg2/SKILL.md").is_file()
    assert (builtin / "deep-research/SKILL.md").is_file()
    assert (builtin / "docx/SKILL.md").is_file()
    assert (builtin / "xlsx/SKILL.md").is_file()
    assert len(skill_files) == 300


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


def test_xlsx_skill_uses_cross_platform_launcher() -> None:
    root = Path(__file__).parents[1]
    xlsx = root / "kimi-cli/src/kimi_cli/skills/xlsx"
    instructions = (xlsx / "SKILL.md").read_text(encoding="utf-8")

    assert "python scripts/xlsx_cli.py" in instructions
    assert "./scripts/Xlsx " not in instructions
    assert (xlsx / "scripts/xlsx_cli.py").is_file()
    assert (xlsx / "scripts/Xlsx-linux-x86_64").is_file()


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
