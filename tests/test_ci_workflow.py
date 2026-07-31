from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow_text() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def test_backend_ruff_checks_only_the_wiki_feature_scope() -> None:
    workflow = _workflow_text()

    assert "run: uv run ruff check src/kimi_cli/wiki tests/wiki" in workflow
    assert "run: uv run ruff format --check src/kimi_cli/wiki tests/wiki" in workflow
    assert "exit-zero" not in workflow


def test_outer_tests_run_from_the_outer_repository_root() -> None:
    workflow = _workflow_text()

    assert "run: kimi-cli/.venv/bin/python -m pytest tests -v --tb=short" in workflow
    assert "working-directory: ." in workflow
    assert "uv run pytest ../tests/" not in workflow
