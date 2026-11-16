"""``kimo evolve`` — self-evolution improvement layer commands.

Evidence collection is always on and free. These commands do the rest of the
loop, and every token-consuming step is gated by the global self-evolution
switch (``default_self_evolve`` in config.toml) — the same switch that makes
adopted improvements take effect in sessions.
"""

from __future__ import annotations

import asyncio
import json

import typer

from kimo_cli.evolution import editor, library
from kimo_cli.evolution import verify as verify_mod

cli = typer.Typer(help="Self-evolution: propose, verify and adopt harness improvements.")


def _load_config():
    from kimo_cli.config import load_config

    return load_config()


def _gate(config) -> None:
    try:
        library.require_enabled(config)
    except library.EvolutionGateError as e:
        typer.echo(f"Refused: {e}", err=True)
        raise typer.Exit(code=2) from None


@cli.command()
def status(
    top: int = typer.Option(10, "--top", help="Number of task groups to show."),
) -> None:
    """Show the evolution switch, library contents, and costliest task evidence."""
    config = _load_config()
    switch = "ON" if library.evolution_enabled(config) else "off"
    typer.echo(f"Self-evolution switch: {switch} (config default_self_evolve)")
    typer.echo(f"Evolution library: {library.evolution_dir()}")
    proposals = library.list_proposals()
    adopted = library.list_adopted()
    typer.echo(f"Proposals pending: {len(proposals)} | adopted: {len(adopted)}")
    if library.experience_file().exists():
        typer.echo(f"Experience index: {library.experience_file()}")

    records = library.iter_all_session_records()
    groups = library.task_groups(records)
    if not groups:
        typer.echo("\nNo replayable task evidence yet (records without a task prompt are skipped).")
        return
    typer.echo(f"\nTop task evidence ({len(groups)} distinct tasks, worst first):")
    for task, turns in groups[:top]:
        fails = sum(1 for t in turns if t.outcome != "completed")
        avg_steps = sum(t.steps for t in turns) / len(turns)
        avg_tokens = sum(t.total_tokens for t in turns) / len(turns)
        typer.echo(
            f"  runs={len(turns)} failed={fails} avg_steps={avg_steps:.1f} "
            f"avg_tokens={avg_tokens:.0f}  {task[:70]}"
        )


@cli.command()
def propose(
    task_pattern: str | None = typer.Option(
        None, "--task-pattern", help="Only consider evidence from tasks containing this text."
    ),
    limit: int = typer.Option(5, "--limit", help="Evidence tasks fed to the editor."),
    model: str | None = typer.Option(None, "--model", help="Editor model override."),
) -> None:
    """Ask the editor model for one focused harness improvement (consumes tokens)."""
    config = _load_config()
    _gate(config)
    try:
        result = asyncio.run(
            editor.propose(config, task_pattern=task_pattern, limit=limit, model_name=model)
        )
    except (RuntimeError, ValueError) as e:
        typer.echo(f"Propose failed: {e}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Proposal written: {result.entry.path}")
    typer.echo(f"Rationale: {result.rationale}")
    _print_entry_files(result.entry)


@cli.command()
def verify(
    proposal: str = typer.Option(..., "--proposal", help="Proposal ID to verify."),
    task_pattern: str | None = typer.Option(None, "--task-pattern"),
    limit: int = typer.Option(3, "--limit", help="Tasks to dual-run (2 headless runs each)."),
) -> None:
    """Dual-run a proposal against real tasks: baseline vs candidate (consumes tokens)."""
    config = _load_config()
    _gate(config)
    try:
        evidence_path = asyncio.run(
            verify_mod.verify(config, proposal, task_pattern=task_pattern, limit=limit)
        )
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        typer.echo(f"Verify failed: {e}", err=True)
        raise typer.Exit(code=1) from None
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    typer.echo(f"Evidence written: {evidence_path}")
    typer.echo(f"Overall verdict suggestion: {evidence['overall_verdict_suggestion']}")
    for t in evidence["tasks"]:
        delta = f" tokens {t['token_delta_pct']:+.1f}%" if t["token_delta_pct"] is not None else ""
        typer.echo(f"  [{t['verdict_suggestion']}] {t['task'][:60]}{delta}")
    typer.echo(f"Caveat: {evidence['caveat']}")


@cli.command()
def adopt(
    proposal: str = typer.Option(..., "--proposal", help="Proposal ID to adopt."),
    force: bool = typer.Option(
        False, "--force", help="Adopt without verification evidence (not recommended)."
    ),
) -> None:
    """Human-approved: move a proposal into the adopted library (applies when switch is on)."""
    config = _load_config()
    _gate(config)
    evidence_path = library.verify_runs_dir() / proposal / "evidence.json"
    if not force and not evidence_path.exists():
        typer.echo(
            f"Refused: no verification evidence at {evidence_path}.\n"
            "Run `kimo evolve verify --proposal "
            f"{proposal}` first, review evidence.json, then adopt. "
            "Use --force only if you verified by other means.",
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        entry = library.adopt_proposal(proposal, evidence_path if evidence_path.exists() else None)
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        typer.echo(f"Adopt failed: {e}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"Adopted: {entry.path}")
    typer.echo("Takes effect in sessions started with the Self-evolve switch on.")


def _print_entry_files(entry: library.Entry) -> None:
    files = [p for p in entry.path.rglob("*") if p.is_file()]
    for p in files:
        typer.echo(f"  - {p.relative_to(entry.path)} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    cli()
