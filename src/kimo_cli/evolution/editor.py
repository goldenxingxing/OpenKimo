"""Editor: propose harness improvements from execution evidence.

The editor is the user's configured default LLM used outside the agent loop
(compaction's ``kosong.step + EmptyToolset`` pattern). It sees the current
system prompt and an evidence summary of costly/failed real tasks, and must
answer with a single JSON object — never prose around it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import kosong
from kosong.message import Message
from kosong.tooling.empty import EmptyToolset

from kimo_cli.evolution import library
from kimo_cli.utils.logging import logger

EDITOR_SYSTEM_PROMPT = """You are the self-evolution editor of an AI agent harness.

You receive:
1. The agent's current system prompt (raw template, may contain ${...} placeholders).
2. An evidence summary of real tasks: their prompts, outcomes, step counts and token usage.

Your job: propose ONE focused, general harness improvement that plausibly helps the agent do
these tasks better (fewer steps/tokens, fewer failures) WITHOUT degrading other work.

Rules:
- Output MUST be a single JSON object and nothing else. No markdown fences, no commentary.
- Schema: {"rationale": str, "system_append": str|null, "skills": {str: str}|null}
  - "rationale": 2-5 sentences. Which task pattern motivated this, what mechanism in the
    append/skill causes the improvement, and its boundary (when it could hurt).
  - "system_append": markdown text appended to the system prompt, or null. Use for general
    behavioural guidance, guardrails, or distilled lessons. Keep under 800 words. No
    placeholders.
  - "skills": map of skill-name -> full SKILL.md content (including YAML frontmatter with name
    and description), or null. Use for procedural knowledge the agent should load on demand.
    Skill names: lowercase letters/digits/hyphens, max 64 chars. Max 2 skills.
- Prefer ONE mechanism over several. An empty proposal (both null) is a valid answer if
  evidence does not support any change.
- Never invent facts about the harness; only use what the system prompt and evidence show.
"""

_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass
class ProposalResult:
    entry: library.Entry
    rationale: str


def _default_system_prompt_path() -> Path:
    from kimo_cli.agentspec import DEFAULT_AGENT_FILE, load_agent_spec

    return load_agent_spec(DEFAULT_AGENT_FILE).system_prompt_path


def _editor_llm(config, model_name: str | None):
    from kimo_cli.auth.oauth import OAuthManager
    from kimo_cli.config import LLMModel, LLMProvider
    from kimo_cli.llm import augment_provider_with_env_vars, create_llm

    name = model_name or config.default_model
    model = provider = None
    if model_name and model_name not in config.models:
        # Falling through to the empty-provider stub below lets env vars
        # (OPENAI_BASE_URL/OPENAI_API_KEY/...) build a working LLM, so the user's
        # explicit --model was silently ignored.
        raise RuntimeError(
            f"Model {model_name!r} is not configured in ~/.kimo/config.toml. "
            f"Configured models: {', '.join(sorted(config.models)) or '(none)'}"
        )
    if name and name in config.models:
        model = config.models[name]
        provider = config.providers.get(model.provider)
    if model is None or provider is None:
        model = LLMModel(provider="", model="", max_context_size=1_000_000)
        provider = LLMProvider(type="openai_legacy", base_url="", api_key="")
    env_overrides = augment_provider_with_env_vars(provider, model)
    if env_overrides:
        provider = provider.model_copy(update=env_overrides)
    llm = create_llm(provider, model, session_id="evolution-editor", oauth=OAuthManager(config))
    if llm is None:
        raise RuntimeError(
            "No LLM available for the evolution editor. Configure a default model "
            "in ~/.kimo/config.toml (as you would for normal agent use)."
        )
    return llm


def _validate_editor_payload(
    system_append: Any, skills: Any
) -> tuple[str | None, dict[str, str] | None]:
    """Type-check and validate the editor's proposal payload.

    The editor output used to be only truthiness-checked and then stringified,
    so a list like ``["a", "b"]`` was written into ``system_append.md`` as
    ``"['a', 'b']"`` and ``0`` looked like an empty proposal.

    Returns:
        The normalized ``(system_append, skills)`` pair.

    Raises:
        ValueError: If any value has the wrong shape.
    """
    if system_append is not None and not isinstance(system_append, str):
        raise ValueError(
            f"Editor produced a non-string system_append: {type(system_append).__name__}"
        )
    if skills is not None and not isinstance(skills, dict):
        raise ValueError(f"Editor produced non-mapping skills: {type(skills).__name__}")

    if isinstance(skills, dict):
        if len(skills) > 2:
            raise ValueError(
                f"Editor produced {len(skills)} skills, but max 2 is allowed; "
                "evidence does not support more than one focused mechanism"
            )
        for name, content in skills.items():
            if not _SKILL_NAME.match(str(name)):
                raise ValueError(f"Editor produced invalid skill name: {name!r}")
            if not isinstance(content, str):
                raise ValueError(
                    f"Editor produced non-string content for skill {name!r}: "
                    f"{type(content).__name__}"
                )
    return (
        system_append if isinstance(system_append, str) else None,
        (
            {str(name): content for name, content in skills.items()}
            if isinstance(skills, dict)
            else None
        ),
    )


def _parse_editor_output(text: str) -> dict:
    """Extract the first JSON object from the model output, tolerating fences."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            raise ValueError("Editor output JSON is not an object")
        return obj
    raise ValueError(f"Editor output contains no JSON object: {text[:200]!r}")


def _evidence_records():
    """All turn evidence across every session root (share + local work dirs).

    ``propose`` used to read only ``get_share_dir()/sessions``, so evidence
    collected in a workspace's ``session-data/`` was invisible and the editor
    always raised ``No replayable task evidence found``. Aggregate every root
    instead, matching ``verify.select_tasks`` and the admin overview.
    """
    return library.iter_all_session_records()


async def propose(
    config,
    *,
    task_pattern: str | None = None,
    limit: int = 5,
    model_name: str | None = None,
) -> ProposalResult:
    """Run one propose cycle; returns the written proposal entry."""
    if limit <= 0:
        raise ValueError("limit must be greater than 0")
    library.require_enabled(config)

    records = _evidence_records()
    groups = library.task_groups(records, pattern=task_pattern)
    if not groups:
        raise RuntimeError(
            "No replayable task evidence found"
            + (f" matching pattern {task_pattern!r}." if task_pattern else ".")
            + " Evidence collection is always on; run some real tasks first."
        )

    summary_lines = []
    source_tasks: list[str] = []
    for task, turns in groups[:limit]:
        fails = sum(1 for t in turns if t.outcome != "completed")
        avg_steps = sum(t.steps for t in turns) / len(turns)
        avg_tokens = sum(t.total_tokens for t in turns) / len(turns)
        summary_lines.append(
            f"- task: {task}\n  runs: {len(turns)}, failed: {fails}, "
            f"avg_steps: {avg_steps:.1f}, avg_total_tokens: {avg_tokens:.0f}"
        )
        source_tasks.append(task)
    evidence_summary = "\n".join(summary_lines)

    prompt_path = _default_system_prompt_path()
    system_prompt_text = prompt_path.read_text(encoding="utf-8")
    # Keep the editor input bounded; the tail carries the behavioural rules,
    # so truncate the head, not the tail.
    if len(system_prompt_text) > 20000:
        system_prompt_text = "\n...[truncated]\n" + system_prompt_text[-20000:]

    user_message = (
        f"## Current system prompt (raw template)\n\n{system_prompt_text}\n\n"
        f"## Evidence summary (real tasks, most problematic first)\n\n{evidence_summary}\n\n"
        "Propose the improvement as a single JSON object per your instructions."
    )

    llm = _editor_llm(config, model_name)
    result = await kosong.step(
        chat_provider=llm.chat_provider,
        system_prompt=EDITOR_SYSTEM_PROMPT,
        toolset=EmptyToolset(),
        history=[Message(role="user", content=user_message)],
    )
    usage = result.usage
    logger.info(
        "Evolution editor used {input} input / {output} output tokens",
        input=usage.input if usage else "?",
        output=usage.output if usage else "?",
    )

    obj = _parse_editor_output(result.message.extract_text(" "))
    rationale = str(obj.get("rationale") or "(no rationale given)")
    # Validate the payload before anything touches the disk: a rejection here
    # must not leave an empty proposal directory behind.
    system_append, skills = _validate_editor_payload(obj.get("system_append"), obj.get("skills"))

    if not system_append and not skills:
        raise RuntimeError(
            "Editor produced an empty proposal (both system_append and skills null). "
            "Evidence does not support a change; rationale: " + rationale
        )

    entry_id = library.new_id()
    entry_path = library.proposals_dir() / entry_id

    try:
        entry_path.mkdir(parents=True, exist_ok=True)
        if system_append:
            (entry_path / "system_append.md").write_text(
                system_append.strip() + "\n", encoding="utf-8"
            )
        if skills:
            for name, content in skills.items():
                skill_dir = entry_path / "skills" / name
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "SKILL.md").write_text(content.strip() + "\n", encoding="utf-8")

        (entry_path / "PROPOSAL.md").write_text(
            f"# Proposal {entry_id}\n\n"
            f"## Rationale\n\n{rationale}\n\n"
            f"## Source tasks\n\n" + "\n".join(f"- {t}" for t in source_tasks) + "\n",
            encoding="utf-8",
        )
        meta = {
            "id": entry_id,
            "status": "proposed",
            "created_at": time.time(),
            "editor_model": model_name or config.default_model,
            "editor_usage": (
                {
                    "input": usage.input,
                    "output": usage.output,
                    "reasoning": usage.reasoning_output,
                }
                if usage
                else None
            ),
            "source_tasks": source_tasks,
            "task_pattern": task_pattern,
        }
        library.save_meta(entry_path, meta)
    except Exception:
        # Roll back the partial entry directory so a later propose doesn't
        # see an orphan proposal with missing meta.json.
        import shutil

        shutil.rmtree(entry_path, ignore_errors=True)
        raise
    entry = library._load_entry(entry_path)
    return ProposalResult(entry=entry, rationale=rationale)
