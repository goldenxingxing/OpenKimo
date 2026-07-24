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

    assert not tuple(builtin.rglob(".env"))
    assert not tuple(builtin.rglob(".DS_Store"))
    assert not tuple(builtin.rglob("__pycache__"))
    assert not tuple(builtin.rglob("*.pyc"))


def test_pyinstaller_collects_builtin_skills() -> None:
    root = Path(__file__).parents[1]
    source = (root / "kimi-cli/src/kimi_cli/utils/pyinstaller.py").read_text()

    assert '"skills/**"' in source
