from pathlib import Path


def test_all_packaged_skills_have_one_source_location() -> None:
    root = Path(__file__).parents[1]
    builtin = root / "kimi-cli/src/kimi_cli/skills"

    assert not (root / "skills").exists()
    assert (builtin / "kimi-cli-help/SKILL.md").is_file()
    assert (builtin / "skill-creator/SKILL.md").is_file()
    assert (builtin / "grounding-dino-seg2/SKILL.md").is_file()


def test_pyinstaller_collects_builtin_skills() -> None:
    root = Path(__file__).parents[1]
    source = (root / "kimi-cli/src/kimi_cli/utils/pyinstaller.py").read_text()

    assert '"skills/**"' in source
