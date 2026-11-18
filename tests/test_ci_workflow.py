from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow_text() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def test_backend_ruff_checks_the_whole_repository() -> None:
    """Widened from src/kimo_cli/wiki + tests/wiki.

    The narrow scope was right while only the wiki feature was clean. It is
    also why six lint errors and thirteen unformatted files accumulated
    everywhere else — including in memory/ and vis/, where this fork does most
    of its work. The repository is clean now, so the check covers it.

    The cost is real and belongs in writing: a sync from upstream can bring
    code that has never been through this configuration, and the fix is a
    format pass on the merge rather than narrowing this again.
    """
    workflow = _workflow_text()

    assert "run: uv run ruff check src tests benchmarks" in workflow
    assert "run: uv run ruff format --check src tests benchmarks" in workflow
    assert "exit-zero" not in workflow


def test_kimo_clis_own_suite_runs() -> None:
    """It did not, for long enough to reach forty-four failures.

    The backend job runs this repository's tests; kimo-cli's own workflows are
    not what gates work here. Two of those failures were real defects, so this
    pins the job that would have caught them — including that it runs on the
    version release.yml ships, not the one .python-version selects.
    """
    workflow = _workflow_text()

    assert "kimo-cli-tests:" in workflow
    assert "run: uv run pytest tests -q --inline-snapshot=disable" in workflow
    assert 'UV_PYTHON: "3.12"' in workflow


def test_outer_tests_run_from_the_outer_repository_root() -> None:
    workflow = _workflow_text()

    assert (
        "run: .venv/bin/python -m pytest tests -v --tb=short --inline-snapshot=disable" in workflow
    )
    assert "working-directory: ." in workflow
    assert "uv run pytest ../tests/" not in workflow
