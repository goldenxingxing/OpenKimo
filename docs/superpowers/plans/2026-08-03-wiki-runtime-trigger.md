# Deterministic Global Wiki Runtime Triggers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing shared global Wiki reliably retrieve relevant knowledge and force the root agent to resolve durable, evidence-backed knowledge opportunities, while deriving all write-admission signals from runtime-observed, turn-bound evidence rather than model claims.

**Architecture:** A root-runtime-owned `WikiTurnCoordinator` keeps bounded, in-memory turn, evidence, checkpoint, and single-use grant state behind an `asyncio.Lock`. `KimoToolset` reports successful typed tool outcomes through a role-aware reporter; `KimoSoul` performs bounded retrieval after an accepted real user prompt and drives root-only checkpoint delivery/resolution at completion boundaries, while foreground/background subagent runners only seal evidence and hand a checkpoint to the root. The existing `WikiManager`, Markdown authority, value gate, Approval flow, transactions, and search cache remain the only persistence path.

**Tech Stack:** Python 3.12+, asyncio, frozen dataclasses, Pydantic 2, stdlib SHA-256/UUID/URL parsing, existing Kimo `Runtime`/`KimoSoul`/`KimoToolset`/subagent/background/notification interfaces, existing `WikiManager` and Approval runtime, pytest/pytest-asyncio, SQLite FTS5 fallback search.

## Global Constraints

- Markdown remains authoritative; SQLite FTS5/WAL is a rebuildable cache.
- Keep exactly one current single-user namespace: `users/default`; future authenticated users must receive isolated coordinators, evidence ledgers, grants, and Wiki namespaces.
- Do not give any subagent the Wiki tool, Wiki prompt/index/retrieval context, checkpoint resolution capability, or mutation authority.
- The root agent must handle qualifying root work, foreground subagent results, background subagent results, explicit remember requests, durable preferences, fixed rules, and cross-session facts.
- Do not automatically extract or write every tool result, every subagent summary, ordinary chat, status/progress, transient state, or unsourced hypotheses.
- Do not add session-end archival, summarization, Wiki proposals, model calls, or commits.
- Do not migrate, scan, import, or package the former workspace-local Wiki pages.
- Do not add Admin Panel changes, a pending-review candidate store, embeddings, a daemon, a watcher, a second extraction model call, or a separate Wiki runtime.
- Normal and AFK modes require the existing compact `wiki.write` Approval; only explicit YOLO skips the popup, after the identical gate.
- A model-supplied `value="high"`, stability flag, source hash, session ID, or checkpoint ID cannot create or widen authority.
- All admission grants are root-only, runtime-created, turn/session/workspace bound, source-hash bound, single-use, and invalidated on cancellation or shutdown.
- Search listings and Shell output may trigger evaluation but cannot independently establish stable or reliable grounding; exact allowed-workspace files and credential-free fetched documents can.
- Empty Wiki or zero search results inject no per-turn block. Automatic retrieval returns at most 3 results, at most 768 UTF-8 bytes of snippet per result, and at most 3 KiB total; the existing startup index remains independently capped at 8 KiB.
- A checkpoint block contains at most 4 checkpoints, 8 evidence references per checkpoint, 1 KiB of summary context per checkpoint, and 6 KiB total; keep at most 16 unresolved checkpoints in memory without silently evicting qualifying foreground work or explicit durable intent.
- Never place raw prompts, raw summaries, raw Shell commands/output, environment values, credentials, absolute machine paths, sensitive filenames, or Wiki page bodies in checkpoint records, persisted manifests, logs, or telemetry.
- Preserve existing secret, path-containment, revision, duplicate, conflict, lock, transaction, recovery, and compact Approval protections.
- Retrieval and trigger failures fail open for the user conversation; forged/stale admission, Approval failures, cancellation, and write failures fail closed for Wiki mutation.
- Support macOS, Windows, Linux/Docker, local KAOS, and remote KAOS; remote sessions may retrieve the shared user Wiki but cannot fabricate a local workspace-file source.
- Do not modify, stage, or commit `packaging/venvstacks.resolved.toml`.

## File Structure

- `kimo-cli/src/kimo_cli/wiki/triggers.py`: frozen trust records, canonical digests, bounded coordinator state, checkpoint lifecycle, single-use admission grants, and managed checkpoint rendering.
- `kimo-cli/src/kimo_cli/wiki/intent.py`: bounded multilingual, negation-aware durable-intent detector over raw accepted user text.
- `kimo-cli/src/kimo_cli/wiki/retrieval.py`: sensitive-input screening, UTF-8 query/result budgets, automatic search, managed retrieval-block rendering, and fail-open result types.
- `kimo-cli/src/kimo_cli/wiki/evidence.py`: typed tool-result normalization, containment/re-read/hash rules, durable/transient classification, URL normalization, and role-aware evidence reporter.
- `kimo-cli/src/kimo_cli/tools/wiki.py`: root-only `persist`/`discard` checkpoint resolution and conversion of runtime grants into the existing `WikiContext` gate.
- `kimo-cli/src/kimo_cli/soul/agent.py`: root coordinator/reporter ownership, fail-open construction, subagent-safe runtime copies, and idempotent shutdown invalidation.
- `kimo-cli/src/kimo_cli/soul/toolset.py`: post-success evidence callback using the actual loaded tool object and actual return value, never a model-provided tool name/status.
- `kimo-cli/src/kimo_cli/soul/kimosoul.py`: accepted-real-turn setup, retrieval injection, root completion checkpoint, one-reminder resolution loop, steer/cancel handling, and no session-end behavior.
- `kimo-cli/src/kimo_cli/subagents/{models.py,store.py,builder.py,core.py,runner.py}`: run-generation identity, isolated subagent reporters, foreground evidence sealing, and root checkpoint attachment.
- `kimo-cli/src/kimo_cli/background/{models.py,store.py,agent_runner.py,manager.py}`: bounded hash-only evidence manifest lifecycle and deduplicated delivery through notification/`TaskOutput`.
- `kimo-cli/src/kimo_cli/notifications/llm.py` and `kimo-cli/src/kimo_cli/tools/background/__init__.py`: attach the same managed checkpoint once when completed background output reaches the root.
- `kimo-cli/tests/wiki/`, `kimo-cli/tests/core/`, and `kimo-cli/tests/tools/`: unit, runtime, security, concurrency, cancellation, and cross-platform coverage.

---

### Task 1: Trusted Evidence, Checkpoint, and Grant State

**Files:**
- Create: `kimo-cli/src/kimo_cli/wiki/triggers.py`
- Create: `kimo-cli/tests/wiki/test_triggers.py`
- Modify: `kimo-cli/src/kimo_cli/wiki/__init__.py`

**Interfaces:**
- Consumes: existing `SourceRef`, `content_hash`, root `provenance_session_id`, optional `workspace_id`, and `telemetry.track(event, **properties)`.
- Produces: `RootTurn`, `EvidenceObservation`, `WikiEvidence`, `WikiCheckpoint`, `WikiAdmissionGrant`, `CheckpointBatch`, `WikiTurnCoordinator`, `canonical_digest(parts: Sequence[str]) -> str`, `WikiTurnCoordinator.begin_turn(raw_text: str, normalized_text: str) -> RootTurn`, `record_evidence(observation: EvidenceObservation) -> WikiEvidence | None`, `create_checkpoint(cause: CheckpointCause, *, evidence_ids: tuple[str, ...] = (), summary_hash: str | None = None, producer_id: str | None = None, run_generation: int | None = None) -> WikiCheckpoint`, `pending_batch() -> CheckpointBatch`, `discard(checkpoint_id: str, reason: CheckpointDiscardReason) -> None`, `cancel_turn(root_turn_id: str) -> None`, and `close() -> None`.

- [ ] **Step 1: Write failing immutable-record, isolation, dedupe, budget, and cancellation tests**

```python
async def test_checkpoint_identity_is_turn_session_workspace_and_evidence_bound(coordinator):
    turn = await coordinator.begin_turn("remember this", "remember this")
    evidence = await coordinator.record_evidence(
        EvidenceObservation(
            root_turn_id=turn.root_turn_id,
            producer_role="root",
            producer_id=None,
            run_generation=None,
            tool_call_id="call-1",
            source_class="workspace-file",
            request_hash="sha256:" + "1" * 64,
            result_hash="sha256:" + "2" * 64,
            logical_paths=("docs/decision.md",),
            source_refs=(WORKSPACE_SOURCE,),
            reliable=True,
            stable_snapshot=True,
            triggering=True,
        )
    )
    checkpoint = await coordinator.create_checkpoint(
        "root_evidence",
        evidence_ids=(evidence.evidence_id,),
        summary_hash="sha256:" + "3" * 64,
    )
    assert checkpoint.root_turn_id == turn.root_turn_id
    assert checkpoint.dedupe_key.startswith("sha256:")
    assert await coordinator.create_checkpoint(
        "root_evidence",
        evidence_ids=(evidence.evidence_id,),
        summary_hash="sha256:" + "3" * 64,
    ) == checkpoint

async def test_coordinator_rejects_cross_session_evidence_and_single_use_replay(two_coordinators):
    left, right = two_coordinators
    turn = await left.begin_turn("x", "x")
    evidence = await left.record_evidence(make_observation(turn.root_turn_id))
    with pytest.raises(WikiTriggerRejected, match="runtime"):
        await right.import_evidence(evidence)
    await left.cancel_turn(turn.root_turn_id)
    assert (await left.pending_batch()).checkpoints == ()

async def test_pending_batch_preserves_explicit_intent_and_bounds_output(coordinator):
    # Create 18 unique checkpoints; explicit intent is always first and only 4 are delivered.
    await seed_checkpoints(coordinator, count=18, explicit_index=17)
    batch = await coordinator.pending_batch()
    assert len(batch.checkpoints) == 4
    assert batch.checkpoints[0].cause == "explicit_user_durable"
    assert len(batch.rendered.encode("utf-8")) <= 6 * 1024
    assert coordinator.unresolved_count == 16
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_triggers.py -q`

Expected: FAIL during collection because `kimo_cli.wiki.triggers` does not exist.

- [ ] **Step 3: Implement frozen records, canonical identifiers, and bounded coordinator state**

```python
CheckpointCause = Literal["root_evidence", "subagent_result", "explicit_user_durable"]
CheckpointState = Literal["pending", "persisting", "discarded", "consumed", "cancelled"]
EvidenceClass = Literal[
    "workspace-file", "workspace-search", "shell-result",
    "web-search", "web-document", "workspace-mutation",
]

@dataclass(frozen=True, slots=True, kw_only=True)
class RootTurn:
    root_turn_id: str
    raw_hash: str
    normalized_hash: str
    started_at: float

@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceObservation:
    root_turn_id: str
    workspace_id: UUID | None
    producer_role: Literal["root", "subagent"]
    producer_id: str | None
    run_generation: int | None
    tool_call_id: str
    source_class: EvidenceClass
    request_hash: str
    result_hash: str
    logical_paths: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    reliable: bool
    stable_snapshot: bool
    triggering: bool

@dataclass(frozen=True, slots=True, kw_only=True)
class WikiEvidence:
    evidence_id: str
    root_turn_id: str
    session_provenance_id: UUID
    workspace_id: UUID | None
    producer_role: Literal["root", "subagent"]
    producer_id: str | None
    run_generation: int | None
    tool_call_id: str
    source_class: EvidenceClass
    request_hash: str
    result_hash: str
    logical_paths: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    reliable: bool
    stable_snapshot: bool
    triggering: bool

@dataclass(frozen=True, slots=True, kw_only=True)
class WikiCheckpoint:
    checkpoint_id: str
    root_turn_id: str
    cause: CheckpointCause
    evidence_ids: tuple[str, ...]
    summary_hash: str | None
    producer_id: str | None
    run_generation: int | None
    dedupe_key: str
    state: CheckpointState
    created_at: float

@dataclass(frozen=True, slots=True, kw_only=True)
class WikiAdmissionGrant:
    checkpoint_id: str
    root_turn_id: str
    session_provenance_id: UUID
    workspace_id: UUID | None
    allowed_source_keys: frozenset[str]
    candidate_hash: str
    high_value: bool
    stable: bool
    grounded: bool
    reliable_source: bool

@dataclass(frozen=True, slots=True, kw_only=True)
class CheckpointBatch:
    checkpoints: tuple[WikiCheckpoint, ...]
    rendered: str
```

Use `uuid.uuid4().hex` for turn/checkpoint/evidence IDs and SHA-256 over length-prefixed UTF-8 components for dedupe keys. Store records only inside the coordinator instance; protect all state transitions with one owning-loop `asyncio.Lock`. Duplicate observations/checkpoints return the existing record. Enforce 16 unresolved checkpoints, but reject/backpressure a 17th qualifying foreground/root checkpoint with `WikiCheckpointBackpressure` rather than evicting it; retain overflow background evidence in Task 4's manifest. `close()` cancels pending records and invalidates grants without touching Wiki files.

- [ ] **Step 4: Run focused tests and static checks**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_triggers.py -q && uv run pyright src/kimo_cli/wiki/triggers.py`

Expected: PASS; mutation attempts on frozen records raise `FrozenInstanceError`, equivalent checkpoint creation deduplicates, separate coordinator instances cannot exchange state, and no rendered batch exceeds its count/byte limits.

- [ ] **Step 5: Commit**

```bash
git add src/kimo_cli/wiki/triggers.py src/kimo_cli/wiki/__init__.py tests/wiki/test_triggers.py
git commit -m "feat(wiki): add trusted turn checkpoint state"
```

### Task 2: Bounded Automatic Retrieval for Accepted Root Prompts

**Files:**
- Create: `kimo-cli/src/kimo_cli/wiki/retrieval.py`
- Create: `kimo-cli/tests/wiki/test_retrieval.py`
- Modify: `kimo-cli/src/kimo_cli/soul/agent.py`
- Modify: `kimo-cli/src/kimo_cli/soul/kimosoul.py`
- Modify: `kimo-cli/tests/core/test_agent.py`
- Modify: `kimo-cli/tests/core/test_kimosoul_slash_commands.py`

**Interfaces:**
- Consumes: `WikiManager.search(query: str, limit: int) -> list[SearchResult]`, `SearchResult.content_hash`, real accepted root `Message`, current `WikiTurnCoordinator`, existing `system(...)` message-part helper.
- Produces: `WikiRetrievalResult(block: str, result_count: int, injected_bytes: int, revision: int | None)`, `build_retrieval_query(raw_text: str, *, max_bytes: int = 512) -> str`, `retrieve_for_turn(manager, coordinator, raw_text, *, synthetic=False, slash_command=False) -> WikiRetrievalResult | None`, and markers `OPENKIMO_WIKI_RETRIEVAL_START/END`.

- [ ] **Step 1: Write failing English/Chinese, empty, secret, marker, and UTF-8 budget tests**

```python
@pytest.mark.parametrize("query", ["atomic wiki recovery", "全局知识检索为什么没有触发"])
async def test_real_root_prompt_retrieves_at_most_three_bounded_pages(manager, coordinator, query):
    seed_search_pages(manager, count=6, token=query.split()[0])
    await coordinator.begin_turn(query, query.strip())
    result = await retrieve_for_turn(manager, coordinator, query)
    assert result is not None
    assert result.result_count <= 3
    assert len(result.block.encode("utf-8")) <= 3 * 1024
    assert result.block.count(OPENKIMO_WIKI_RETRIEVAL_START) == 1
    assert all(len(snippet.encode("utf-8")) <= 768 for snippet in extract_snippets(result.block))
    result.block.encode("utf-8").decode("utf-8")

@pytest.mark.parametrize(
    ("text", "reason"),
    [("/help", "slash_command"), ("a", "too_short"), ("api_key=secret-value", "sensitive")],
)
async def test_retrieval_skips_non_user_or_sensitive_input(manager, coordinator, text, reason):
    result = await retrieve_for_turn(manager, coordinator, text, slash_command=text.startswith("/"))
    assert result is None
    assert coordinator.last_retrieval_outcome == reason

async def test_empty_wiki_and_zero_hits_inject_nothing(empty_manager, coordinator):
    assert await retrieve_for_turn(empty_manager, coordinator, "durable architecture") is None
```

- [ ] **Step 2: Run retrieval tests and verify RED**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_retrieval.py tests/core/test_agent.py tests/core/test_kimosoul_slash_commands.py -q`

Expected: FAIL because the retrieval module and pre-model retrieval hook do not exist.

- [ ] **Step 3: Implement query screening, whole-code-point budgets, and managed reference rendering**

```python
RETRIEVAL_MAX_QUERY_BYTES = 512
RETRIEVAL_MAX_RESULTS = 3
RETRIEVAL_MAX_SNIPPET_BYTES = 768
RETRIEVAL_MAX_BLOCK_BYTES = 3 * 1024

@dataclass(frozen=True, slots=True)
class WikiRetrievalResult:
    block: str
    result_count: int
    injected_bytes: int
    revision: int | None

def build_retrieval_query(raw_text: str, *, max_bytes: int = 512) -> str:
    normalized = " ".join(raw_text.split())
    return truncate_utf8(normalized, max_bytes=max_bytes).strip()
```

Reject empty/whitespace, fewer than 2 Unicode code points, slash commands, synthetic prompts, subagent role, prompt-hook rejection, and text matching existing sensitive token detectors. Call `manager.search(query, 3)` in `asyncio.to_thread`; render only logical path, title, one-line summary, bounded snippet, revision, and content hash. Label the block as untrusted retrieved reference material. Store only result path/revision/hash in the coordinator ledger. Catch all search/render failures, record a safe skip/failure category, and continue without injection.

In `Runtime.create`, construct `WikiTurnCoordinator` only for a root runtime whose Wiki initialized successfully, using the runtime's provenance session UUID and optional workspace UUID; store it as `Runtime.wiki_coordinator`. Set this field to `None` in `copy_for_subagent`. In `KimoSoul.run`, move real-turn coordinator setup after `UserPromptSubmit` acceptance and after slash-command detection, but before `_turn`. Append the retrieval block as a separate managed `system(...)` part after the raw user message and before the first model step. Do not pass it to conversation hash/durable-intent detection. Ensure a continuation sees the existing block rather than appending another marker.

- [ ] **Step 4: Run retrieval and existing prompt-loop regression tests**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_retrieval.py tests/core/test_agent.py tests/core/test_kimosoul_slash_commands.py tests/core/test_kimosoul_steer.py tests/wiki/test_search.py tests/wiki/test_search_recovery.py -q`

Expected: PASS; empty Wiki is a no-op, FTS failure uses the manager's Markdown fallback, one accepted root prompt emits at most one retrieval block, and slash/synthetic/subagent inputs never search.

- [ ] **Step 5: Commit**

```bash
git add src/kimo_cli/wiki/retrieval.py src/kimo_cli/soul/agent.py src/kimo_cli/soul/kimosoul.py tests/wiki/test_retrieval.py tests/core/test_agent.py tests/core/test_kimosoul_slash_commands.py
git commit -m "feat(wiki): retrieve global knowledge per root turn"
```

### Task 3: Typed Tool Evidence and Root-Agent Completion Checkpoints

**Files:**
- Create: `kimo-cli/src/kimo_cli/wiki/evidence.py`
- Create: `kimo-cli/tests/wiki/test_evidence.py`
- Create: `kimo-cli/tests/core/test_wiki_root_checkpoint.py`
- Modify: `kimo-cli/src/kimo_cli/soul/agent.py`
- Modify: `kimo-cli/src/kimo_cli/soul/toolset.py`
- Modify: `kimo-cli/src/kimo_cli/soul/kimosoul.py`
- Modify: `kimo-cli/tests/core/test_toolset.py`

**Interfaces:**
- Consumes: actual loaded instances of `ReadFile`, `ReadMediaFile`, `WriteFile`, `StrReplaceFile`, `Glob`, `Grep`, `Shell`, `SearchWeb`, and `FetchURL`; actual `ToolReturnValue`; `Runtime.role`; workspace registry/containment; current root turn.
- Produces: `WikiEvidenceReporter.observe(tool: ToolType, arguments: JsonType, result: ToolReturnValue, *, tool_call_id: str) -> Awaitable[WikiEvidence | None]`, `seal_root_completion(conclusion: str) -> WikiCheckpoint | None`, `Runtime.wiki_coordinator`, `Runtime.wiki_evidence_reporter`, and `KimoToolset(runtime: Runtime)`.

- [ ] **Step 1: Write failing typed evidence and source-integrity tests**

```python
async def test_read_file_records_exact_workspace_source(runtime, read_file, coordinator, tmp_path):
    path = tmp_path / "workspace" / "docs" / "decision.md"
    path.parent.mkdir(parents=True)
    path.write_bytes("稳定结论\n".encode())
    result = await read_file(ReadParams(path=str(path)))
    evidence = await runtime.wiki_evidence_reporter.observe(
        read_file, {"path": str(path)}, result, tool_call_id="read-1"
    )
    assert evidence.source_class == "workspace-file"
    assert evidence.logical_paths == ("docs/decision.md",)
    assert evidence.source_refs[0].content_hash == content_hash(path.read_bytes())
    assert evidence.reliable and evidence.stable_snapshot

async def test_model_name_or_success_flag_cannot_forge_evidence(runtime, fake_tool, coordinator):
    fake_tool.name = "ReadFile"
    result = ToolOk(output="pretend durable content")
    assert await runtime.wiki_evidence_reporter.observe(
        fake_tool, {"path": "docs/decision.md", "success": True}, result, tool_call_id="fake"
    ) is None

@pytest.mark.parametrize("outcome", [ToolError(message="failed"), ToolOk(output=""), CANCELLED])
async def test_failed_empty_or_cancelled_results_create_no_evidence(reporter, shell, outcome):
    assert await observe_outcome(reporter, shell, outcome) is None

async def test_root_completion_creates_one_checkpoint_without_subagent(root_soul, coordinator):
    await run_scripted_root(root_soul, [read_call("docs/config.md"), final("Reusable rule")])
    pending = await coordinator.pending_batch()
    assert [item.cause for item in pending.checkpoints] == ["root_evidence"]
    assert pending.checkpoints[0].summary_hash == content_hash(b"Reusable rule")
```

- [ ] **Step 2: Run evidence/root bridge tests and verify RED**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_evidence.py tests/core/test_wiki_root_checkpoint.py tests/core/test_toolset.py -q`

Expected: FAIL because `WikiEvidenceReporter`, runtime reporter binding, and root completion sealing are absent.

- [ ] **Step 3: Implement class-identity observation and runtime re-read/hash rules**

```python
class WikiEvidenceReporter:
    def __init__(
        self,
        coordinator: WikiTurnCoordinator,
        runtime: Runtime,
        *,
        producer_role: Literal["root", "subagent"],
        producer_id: str | None = None,
        run_generation: int | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._runtime = runtime
        self._producer_role = producer_role
        self._producer_id = producer_id
        self._run_generation = run_generation

    async def observe(self, tool, arguments, result, *, tool_call_id):
        if isinstance(result, ToolError) or not _has_non_empty_output(result):
            return None
        if isinstance(tool, (ReadFile, ReadMediaFile)):
            return await self._workspace_file(tool, arguments, tool_call_id, mutation=False)
        if isinstance(tool, (WriteFile, StrReplaceFile)):
            return await self._workspace_file(tool, arguments, tool_call_id, mutation=True)
        if isinstance(tool, (Glob, Grep)):
            return self._workspace_search(arguments, result, tool_call_id)
        if isinstance(tool, Shell):
            return self._shell(arguments, result, tool_call_id)
        if isinstance(tool, SearchWeb):
            return self._web_search(arguments, result, tool_call_id)
        if isinstance(tool, FetchURL):
            return self._web_document(arguments, result, tool_call_id)
        return None
```

For reads/mutations, ignore returned path claims: canonicalize the runtime-known requested target, require it below the registered current workspace, reject symlinks/sensitive files, re-open exact bytes after success, convert to a relative POSIX path, and create a `SourceRef(kind="workspace-file", ...)`. On local mutation, re-read only after the tool returns success. For remote KAOS, do not create a workspace-file source because local exact-byte verification is unavailable. For Shell, retain only normalized invocation/result hashes and inspect structured result/exit status; mark clock/process/current-status/test-count-only patterns `triggering=False`. For Glob/Grep/SearchWeb, record discovery evidence with `reliable=False`, `stable_snapshot=False`. For `FetchURL`, normalize scheme/host/path, reject userinfo and sensitive query/fragment parameters, hash exact bounded response bytes, and record `web-document`; never log the URL.

Construct `KimoToolset(runtime)` in `load_agent`. After the actual tool call returns and before `ToolResult` is returned, await `runtime.wiki_evidence_reporter.observe(tool, arguments, ret, tool_call_id=tool_call.id)`; isolate reporter exceptions so the tool result is unchanged. Same-step deduplicated calls reuse the first result but do not record a second evidence record.

At the root no-tool completion boundary, call `coordinator.seal_root_completion(result.message.extract_text("\n"))`. Create one `root_evidence` checkpoint only when the active real turn has triggering evidence, a non-empty reusable conclusion, and at least one non-transient evidence record. Repeated completion attempts use the same conclusion/evidence digest. If the root is already resolving an equivalent subagent checkpoint, attach new root evidence to it instead of creating another checkpoint.

- [ ] **Step 4: Run source-class, root-loop, concurrency, and existing tool regressions**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_evidence.py tests/core/test_wiki_root_checkpoint.py tests/core/test_toolset.py tests/tools/test_read_file.py tests/tools/test_write_file.py tests/tools/test_str_replace_file.py tests/tools/test_shell_bash.py tests/tools/test_fetch_url.py -q`

Expected: PASS; root work produces a checkpoint without a subagent, failed/empty/transient results do not, Shell/search alone cannot set reliable/stable, concurrent root results stay in one turn ledger, and tool behavior is unchanged if evidence capture fails.

- [ ] **Step 5: Commit**

```bash
git add src/kimo_cli/wiki/evidence.py src/kimo_cli/soul/agent.py src/kimo_cli/soul/toolset.py src/kimo_cli/soul/kimosoul.py tests/wiki/test_evidence.py tests/core/test_wiki_root_checkpoint.py tests/core/test_toolset.py
git commit -m "feat(wiki): capture root runtime evidence"
```

### Task 4: Foreground and Background Subagent Return Checkpoints

**Files:**
- Create: `kimo-cli/tests/core/test_wiki_subagent_checkpoint.py`
- Modify: `kimo-cli/src/kimo_cli/subagents/models.py`
- Modify: `kimo-cli/src/kimo_cli/subagents/store.py`
- Modify: `kimo-cli/src/kimo_cli/subagents/builder.py`
- Modify: `kimo-cli/src/kimo_cli/subagents/core.py`
- Modify: `kimo-cli/src/kimo_cli/subagents/runner.py`
- Modify: `kimo-cli/src/kimo_cli/background/models.py`
- Modify: `kimo-cli/src/kimo_cli/background/store.py`
- Modify: `kimo-cli/src/kimo_cli/background/agent_runner.py`
- Modify: `kimo-cli/src/kimo_cli/background/manager.py`
- Modify: `kimo-cli/src/kimo_cli/notifications/llm.py`
- Modify: `kimo-cli/src/kimo_cli/tools/background/__init__.py`
- Modify: `kimo-cli/src/kimo_cli/soul/agent.py`
- Modify: `kimo-cli/src/kimo_cli/soul/kimosoul.py`
- Modify: `kimo-cli/tests/tools/test_agent_tool.py`
- Modify: `kimo-cli/tests/tools/test_background_tools.py`

**Interfaces:**
- Consumes: `WikiEvidenceReporter` from Task 3, `WikiTurnCoordinator.create_checkpoint`, foreground final summary, background task store/notification/`TaskOutput` lifecycle.
- Produces: monotonic `run_generation: int` on `AgentInstanceRecord`; `PersistedEvidenceRef`; `WikiEvidenceManifest`; `TaskConsumerState.delivered_wiki_checkpoint_keys: list[str]`; `BackgroundTaskStore.write_wiki_evidence_manifest(task_id: str, manifest: WikiEvidenceManifest) -> None`; `read_wiki_evidence_manifest(task_id: str) -> WikiEvidenceManifest | None`; `BackgroundTaskManager.checkpoint_block_for_task(task_id: str) -> str`; `build_notification_message(view, runtime, *, checkpoint_block: str = "") -> Message`; and `WikiTurnCoordinator.accept_subagent_result(*, agent_id: str, run_generation: int, summary_hash: str, evidence: Sequence[WikiEvidence | PersistedEvidenceRef], receiving_root_turn_id: str, task_id: str | None = None) -> WikiCheckpoint | None`.

- [ ] **Step 1: Write failing foreground/background, replay, resume-generation, and no-Wiki tests**

```python
async def test_foreground_evidence_summary_creates_root_checkpoint(runtime, agent_tool, coordinator):
    result = await run_foreground_agent_with_read(agent_tool, "docs/architecture.md")
    assert not result.is_error
    assert result.output.count("OPENKIMO_WIKI_CHECKPOINT_START") == 1
    checkpoint = (await coordinator.pending_batch()).checkpoints[0]
    assert checkpoint.cause == "subagent_result"
    assert checkpoint.producer_id == extract_agent_id(result.output)

async def test_no_tool_failed_cancelled_or_timed_out_subagent_has_no_checkpoint(
    subagent_case_runner, coordinator
):
    for terminal in ("no_tool", "failed", "killed", "lost", "timed_out"):
        result = await subagent_case_runner(terminal)
        assert "OPENKIMO_WIKI_CHECKPOINT_START" not in result.output
    assert not coordinator.has_pending

async def test_notification_and_repeated_task_output_deliver_background_checkpoint_once(
    runtime, completed_background_agent, notification_view, task_output
):
    task_id = completed_background_agent
    block = await runtime.background_tasks.checkpoint_block_for_task(task_id)
    notification = build_notification_message(
        notification_view, runtime, checkpoint_block=block
    )
    first = await task_output(TaskOutputParams(task_id=task_id))
    second = await task_output(TaskOutputParams(task_id=task_id))
    combined = notification.extract_text("\n") + first.output + second.output
    assert combined.count("OPENKIMO_WIKI_CHECKPOINT_START") == 1

def test_builtin_and_custom_subagent_cannot_load_or_call_wiki(root_runtime):
    for agent_type in ("coder", "explore", "plan"):
        agent = build_subagent(root_runtime, agent_type)
        assert agent.toolset.find("Wiki") is None
        assert "OPENKIMO_GLOBAL_WIKI_START" not in agent.system_prompt
        assert "OPENKIMO_WIKI_RETRIEVAL_START" not in agent.system_prompt
```

- [ ] **Step 2: Run subagent bridge tests and verify RED**

Run: `cd kimo-cli && uv run pytest tests/core/test_wiki_subagent_checkpoint.py tests/tools/test_agent_tool.py tests/tools/test_background_tools.py tests/core/test_runtime_roles.py -q`

Expected: FAIL because subagent evidence is not sealed, run generations/manifests do not exist, and subagent runtime copies still inherit Wiki context.

- [ ] **Step 3: Isolate subagent runtime and seal foreground evidence at successful return**

Add `run_generation` to subagent metadata with backward-compatible default `0`; increment it atomically in `SubagentStore.begin_run(agent_id)`. Pass it through `SubagentRunSpec`, `SubagentBuilder.build_builtin_instance`, and `Runtime.copy_for_subagent`. The copy must set `wiki=None`, `wiki_tool_context=None`, `wiki_coordinator=None`, and create only a reporter bound to the root coordinator with `producer_role="subagent"`, agent ID, and run generation. `_apply_spec_to_builtin_args` must blank `KIMO_WIKI_CONTEXT` for `runtime.role == "subagent"`. Add a defense in `Wiki.__call__` returning `ToolError` unless `runtime.role == "root"`, including for custom specs that accidentally list Wiki.

After a foreground run succeeds and produces a non-empty final summary, seal that run's evidence and call:

```python
checkpoint = await root_coordinator.accept_subagent_result(
    agent_id=agent_id,
    run_generation=run_generation,
    summary_hash=content_hash(final_response.encode("utf-8")),
    evidence=sealed_evidence,
    receiving_root_turn_id=root_coordinator.active_turn_id,
)
```

Append only the coordinator-rendered managed checkpoint block after the normal summary. Failed, cancelled, timed-out, empty, or no-evidence runs return their current result unchanged.

- [ ] **Step 4: Persist hash-only background evidence and deduplicate all root delivery paths**

```python
class PersistedEvidenceRef(BaseModel):
    evidence_id: str
    source_class: EvidenceClass
    request_hash: str
    result_hash: str
    logical_paths: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    reliable: bool
    stable_snapshot: bool

class WikiEvidenceManifest(BaseModel):
    version: Literal[1] = 1
    agent_id: str
    task_id: str
    run_generation: int
    summary_hash: str
    evidence: tuple[PersistedEvidenceRef, ...]
    status: Literal["sealed"] = "sealed"
```

Persist only IDs, hashes, source class, portable source refs, stable/reliable flags, and run generation in `wiki-evidence.json` beside the background task; never duplicate output or candidate Markdown. Write it before `_mark_task_completed`. Add async `BackgroundTaskManager.checkpoint_block_for_task(task_id: str) -> str` to authenticate the sealed manifest with the root coordinator for the current receiving turn and atomically record its dedupe key in `TaskConsumerState.delivered_wiki_checkpoint_keys`. In `KimoSoul._step`'s async notification callback, obtain this block before calling the synchronous `build_notification_message(view, runtime, checkpoint_block=block)`. `TaskOutput.__call__` uses the same manager method. Repeated notification reconciliation and `TaskOutput` calls return ordinary output without a second block. A restart may load a sealed manifest, but must create a fresh receiving-turn checkpoint; it cannot create a grant by itself. Failed/killed/lost/timed-out tasks delete/ignore any incomplete manifest.

- [ ] **Step 5: Run foreground/background/restart/resume tests and existing subagent suite**

Run: `cd kimo-cli && uv run pytest tests/core/test_wiki_subagent_checkpoint.py tests/tools/test_agent_tool.py tests/tools/test_background_tools.py tests/core/test_subagent_builder.py tests/core/test_subagent_resume_e2e.py tests/background -q`

Expected: PASS; foreground and background evidence-bearing results create one root checkpoint, resuming increments generation, notification plus repeated `TaskOutput` cannot replay it, and no subagent exposes Wiki read/write/checkpoint operations or prompt context.

- [ ] **Step 6: Commit**

```bash
git add src/kimo_cli/subagents src/kimo_cli/background src/kimo_cli/notifications/llm.py src/kimo_cli/tools/background/__init__.py src/kimo_cli/soul/agent.py src/kimo_cli/soul/kimosoul.py tests/core/test_wiki_subagent_checkpoint.py tests/tools/test_agent_tool.py tests/tools/test_background_tools.py
git commit -m "feat(wiki): bridge subagent evidence to root"
```

### Task 5: Explicit Durable Intent and Deterministic Root Resolution Loop

**Files:**
- Create: `kimo-cli/src/kimo_cli/wiki/intent.py`
- Create: `kimo-cli/tests/wiki/test_intent.py`
- Create: `kimo-cli/tests/core/test_wiki_checkpoint_resolution.py`
- Modify: `kimo-cli/src/kimo_cli/wiki/triggers.py`
- Modify: `kimo-cli/src/kimo_cli/tools/wiki.py`
- Modify: `kimo-cli/src/kimo_cli/soul/kimosoul.py`
- Modify: `kimo-cli/src/kimo_cli/agents/default/system.md`
- Modify: `kimo-cli/tests/tools/test_wiki_tool.py`

**Interfaces:**
- Consumes: exact raw accepted user text, `WikiTurnCoordinator`, root `KimoSoul` no-tool completion boundary, existing Wiki tool.
- Produces: `DurableIntent(family, raw_hash, normalized_hash)`, `detect_durable_intent(raw_text: str) -> DurableIntent | None`, `Wiki Params.operation += "discard"`, required `checkpoint_id` for `remember`/`ingest`, `discard_reason: Literal["low_value","unstable","ungrounded","duplicate","not_reusable"]`, and one managed continuation before deterministic `unresolved` discard.

- [ ] **Step 1: Write failing multilingual/negation and one-reminder tests**

```python
@pytest.mark.parametrize(
    ("text", "family"),
    [
        ("Remember this release rule for future sessions.", "remember"),
        ("写入知识库：发布前必须运行完整测试。", "remember"),
        ("From now on, I prefer concise reports.", "preference"),
        ("以后都使用英文提交信息。", "preference"),
        ("Our permanent release rule is signed tags only.", "fixed_rule"),
        ("后续所有工作区都要遵守这个规则。", "fixed_rule"),
    ],
)
def test_durable_intent_positive_patterns(text, family):
    result = detect_durable_intent(text)
    assert result is not None and result.family == family

@pytest.mark.parametrize(
    "text",
    ["Do not remember this", "only for this run", "不要记住", "这次临时使用", "build is running"],
)
def test_durable_intent_negation_and_temporary_forms_do_not_match(text):
    assert detect_durable_intent(text) is None

async def test_unresolved_checkpoint_gets_one_continuation_then_discard(root_soul, coordinator):
    await root_soul.run("Remember this stable rule")
    assert scripted_llm.call_count == 2
    assert coordinator.terminal_reason == "unresolved"
    assert not coordinator.has_pending
```

- [ ] **Step 2: Run intent/resolution tests and verify RED**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_intent.py tests/core/test_wiki_checkpoint_resolution.py tests/tools/test_wiki_tool.py -q`

Expected: FAIL because the detector, discard operation, checkpoint IDs, and resolution continuation do not exist.

- [ ] **Step 3: Implement bounded durable-intent detection and pre-model checkpoint creation**

Use reviewed regex families over at most 4 KiB of raw user-authored text. Check negation/temporary markers before positive markers. Normalize only Unicode whitespace/case for the normalized hash; preserve the raw hash separately. Return one coalesced result in precedence order `remember`, `preference`, `fixed_rule`. After prompt-hook acceptance and before retrieval/first model step, create exactly one `explicit_user_durable` checkpoint containing the real turn/session/workspace binding, raw/normalized hashes, and family. Steers accepted inside the same real turn may add one durable checkpoint using their exact raw hashes, but synthetic prompts and checkpoint reminders cannot.

- [ ] **Step 4: Add managed resolution instructions, discard, and the bounded continuation loop**

```python
class Params(BaseModel):
    operation: Literal["search", "read", "remember", "ingest", "lint", "discard"]
    checkpoint_id: str | None = None
    discard_reason: Literal["low_value", "unstable", "ungrounded", "duplicate", "not_reusable"] | None = None
    # existing query/page/candidate/source/limit fields remain
```

Require the root to call `remember`/`ingest` with `checkpoint_id` or call `discard`. Discard stores no candidate/page and emits one terminal coordinator transition. Render only checkpoint ID, cause, counts, portable source refs, and the required persist/discard action between `OPENKIMO_WIKI_CHECKPOINT_START/END`; never render raw summary/user text.

In `_agent_loop`, before returning a root `no_tool_calls` outcome:

1. Seal a root-evidence checkpoint if eligible.
2. If newly pending checkpoints have not been delivered, append one managed internal message and continue.
3. If a delivered checkpoint remains unresolved at the next completion, append exactly one reminder and continue.
4. If it remains unresolved at the following completion, discard with `unresolved` internally and finish; unavailable Wiki resolves as `unavailable` without another model call.

Checkpoint synthetic messages bypass `UserPromptSubmit`, retrieval, durable-intent detection, conversation hashes, and title generation. Explicit intent has delivery priority. Repeated completion and steer processing must not duplicate markers/checkpoints.

- [ ] **Step 5: Run intent, continuation, slash, steer, repeat, and turn-balance tests**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_intent.py tests/core/test_wiki_checkpoint_resolution.py tests/tools/test_wiki_tool.py tests/core/test_kimosoul_steer.py tests/core/test_kimosoul_repeat.py tests/core/test_kimosoul_turn_balance.py -q`

Expected: PASS; English/Chinese durable intent creates one checkpoint, negated/temporary text creates none, each checkpoint is persist-or-discard resolved, and an uncooperative model adds at most one reminder and never loops.

- [ ] **Step 6: Commit**

```bash
git add src/kimo_cli/wiki/intent.py src/kimo_cli/wiki/triggers.py src/kimo_cli/tools/wiki.py src/kimo_cli/soul/kimosoul.py src/kimo_cli/agents/default/system.md tests/wiki/test_intent.py tests/core/test_wiki_checkpoint_resolution.py tests/tools/test_wiki_tool.py
git commit -m "feat(wiki): require root checkpoint resolution"
```

### Task 6: Single-Use Trusted Admission and Approval/YOLO/AFK Integration

**Files:**
- Create: `kimo-cli/tests/tools/test_wiki_admission.py`
- Modify: `kimo-cli/src/kimo_cli/wiki/triggers.py`
- Modify: `kimo-cli/src/kimo_cli/tools/wiki.py`
- Modify: `kimo-cli/src/kimo_cli/wiki/value_gate.py`
- Modify: `kimo-cli/tests/tools/test_wiki_approval.py`
- Modify: `kimo-cli/tests/wiki/test_value_gate.py`

**Interfaces:**
- Consumes: `WikiCheckpoint`, current evidence ledger, candidate/page `SourceRef`s, existing `WikiManager.prepare/ingest/commit`, `WikiContext`, `Approval.request("Wiki", "wiki.write", ...)`.
- Produces: `WikiTurnCoordinator.reserve_grant(checkpoint_id: str, candidate: WikiCandidate, source: CurrentSource | None) -> WikiAdmissionGrant`, `finish_grant(checkpoint_id, *, outcome, candidate_hash)`, `release_retry(checkpoint_id, candidate_hash)`, and `WikiContext.from_grant(grant, operation)`; model `candidate.value` is ignored for authorization.

- [ ] **Step 1: Write failing forgery, stale source, cause-specific, single-use, and mode tests**

```python
async def test_model_high_stable_hash_and_checkpoint_claims_cannot_forge_grant(wiki_tool):
    forged = Params(
        operation="remember",
        checkpoint_id="attacker-chosen",
        candidate=candidate(value="high", source=forged_workspace_source()),
    )
    result = await wiki_tool(forged)
    assert result.is_error
    assert wiki_revision() == 0

async def test_shell_or_search_checkpoint_cannot_pass_without_exact_durable_source(
    wiki_tool, shell_and_search_checkpoint, unsourced_candidate
):
    checkpoint = shell_and_search_checkpoint
    result = await wiki_tool(params_for(checkpoint, unsourced_candidate))
    assert result.is_error and "stable" in result.message.lower()

async def test_changed_file_hash_fails_stale_and_fresh_read_can_retry(
    wiki_tool, file_checkpoint_factory, approve_and_run
):
    checkpoint, source_path = await file_checkpoint_factory.read_current()
    source_path.write_text("changed", encoding="utf-8")
    assert (await wiki_tool(params_for(checkpoint))).is_error
    checkpoint = await file_checkpoint_factory.read_again(source_path)
    assert not (await approve_and_run(params_for(checkpoint))).is_error

@pytest.mark.parametrize(("mode", "approval_count"), [("normal", 1), ("afk", 1), ("yolo", 0)])
async def test_mode_changes_only_popup_not_gate(mode, approval_count, admitted_params):
    result = await run_wiki(mode, admitted_params)
    assert not result.is_error
    assert approval_runtime.request_count == approval_count

async def test_concurrent_calls_can_consume_checkpoint_once(wiki_tool, admitted_params):
    results = await asyncio.gather(wiki_tool(admitted_params), wiki_tool(admitted_params))
    assert sum(not result.is_error for result in results) == 1
```

- [ ] **Step 2: Run admission tests and verify RED**

Run: `cd kimo-cli && uv run pytest tests/tools/test_wiki_admission.py tests/tools/test_wiki_approval.py tests/wiki/test_value_gate.py -q`

Expected: FAIL because current `WikiToolContext` uses broad booleans and writes do not authenticate a checkpoint or exact evidence.

- [ ] **Step 3: Replace broad turn booleans with atomic grant reservation and exact source matching**

Canonicalize all candidate plus page sources into source keys. Under the coordinator lock, require a pending active-root checkpoint, matching session/turn/workspace, exact conclusion/summary binding, and unconsumed evidence. Atomically transition `pending -> persisting` before returning a grant. Re-open each workspace file via registry containment and compare its current bytes to the captured hash; normalize and compare credential-free web URL plus exact fetched-content hash; conversation sources are allowed only for `explicit_user_durable` and must match that turn's raw or normalized accepted-user hash.

Cause-specific grant rules:

```python
if checkpoint.cause == "explicit_user_durable":
    high_value = stable = True
    reliable_source = all_sources_match_user_or_exact_evidence
elif checkpoint.cause in {"root_evidence", "subagent_result"}:
    high_value = root_selected_persist_for_exact_checkpoint
    stable = any(e.stable_snapshot for e in admitted_evidence)
    reliable_source = bool(admitted_evidence) and all(e.reliable for e in admitted_evidence)
grounded = every_candidate_and_page_source_has_matching_unconsumed_evidence
```

Ignore `WikiCandidate.value` when deriving the grant; retain the field only for backward-compatible schema parsing. Build `WikiContext` exclusively from the grant. Do not use `ContextVar` as authority. Reject expired/cancelled/cross-turn/cross-session/cross-workspace/replayed checkpoint IDs with safe generic errors.

- [ ] **Step 4: Preserve gate/Approval/transaction order and terminal outcomes**

Keep the exact order: reserve grant; construct trusted gate context; run source/secret/path/page/novelty/duplicate/conflict validation; discard medium/low/unstable/ungrounded/unsafe/duplicate immediately; request compact Approval in normal and AFK; skip only the popup in YOLO; acquire writer lock and revalidate/commit; consume grant/evidence. Decline, Approval cancellation/delivery failure, and unsafe/duplicate decisions consume and discard the candidate with no file/queue. A retryable revision conflict may call `release_retry` once for the identical `candidate_hash` in the active turn; it cannot authorize changed candidate content.

Remove admission use of `candidate_high_value`, `stable`, and `reliable_source` from `WikiToolContext`; keep only provenance/session compatibility needed for read-only description until callers migrate. Enforce `runtime.role == "root"` before every checkpoint, write, or discard operation. Keep normal allow-once/session-wide Approval behavior unchanged.

- [ ] **Step 5: Run admission, Approval, manager, transaction, and concurrency tests**

Run: `cd kimo-cli && uv run pytest tests/tools/test_wiki_admission.py tests/tools/test_wiki_approval.py tests/tools/test_wiki_tool.py tests/wiki/test_value_gate.py tests/wiki/test_manager.py tests/wiki/test_transaction.py tests/wiki/test_recovery.py -q`

Expected: PASS; model fields cannot widen authority, exact stale sources fail, AFK requests Approval, YOLO only skips Approval, duplicate/unsafe candidates never prompt, and concurrent calls commit at most once.

- [ ] **Step 6: Commit**

```bash
git add src/kimo_cli/wiki/triggers.py src/kimo_cli/tools/wiki.py src/kimo_cli/wiki/value_gate.py tests/tools/test_wiki_admission.py tests/tools/test_wiki_approval.py tests/wiki/test_value_gate.py
git commit -m "feat(wiki): authenticate runtime admission grants"
```

### Task 7: Privacy-Safe Trigger Observability

**Files:**
- Create: `kimo-cli/src/kimo_cli/wiki/telemetry.py`
- Create: `kimo-cli/tests/wiki/test_trigger_observability.py`
- Modify: `kimo-cli/src/kimo_cli/wiki/retrieval.py`
- Modify: `kimo-cli/src/kimo_cli/wiki/evidence.py`
- Modify: `kimo-cli/src/kimo_cli/wiki/triggers.py`
- Modify: `kimo-cli/src/kimo_cli/tools/wiki.py`

**Interfaces:**
- Consumes: existing `kimo_cli.telemetry.track`, safe coordinator IDs/counts/enums/revisions/durations.
- Produces: `track_wiki_event(event: WikiEventName, **safe_fields) -> None` with an allowlist for `wiki_retrieval_hit`, `wiki_retrieval_miss`, `wiki_retrieval_skipped`, `wiki_evidence_recorded`, `wiki_checkpoint_created`, `wiki_checkpoint_resolved`, `wiki_candidate_discarded`, `wiki_approval_requested`, `wiki_committed`, and `wiki_trigger_failed`.

- [ ] **Step 1: Write failing event-order, exactly-once, and privacy tests**

```python
async def test_success_event_sequence_is_deterministic(captured_events, admitted_turn):
    await admitted_turn.run()
    assert event_names(captured_events) == [
        "wiki_retrieval_miss",
        "wiki_evidence_recorded",
        "wiki_checkpoint_created",
        "wiki_approval_requested",
        "wiki_committed",
        "wiki_checkpoint_resolved",
    ]

def test_wiki_events_never_contain_sensitive_payloads(captured_events):
    serialized = json.dumps(captured_events)
    for forbidden in (
        "/Users/qunwei", "C:\\\\Users", "api_key", "Bearer ",
        "raw command", "raw prompt", "page body", "subagent summary",
    ):
        assert forbidden not in serialized

async def test_telemetry_failure_never_changes_retrieval_gate_or_commit(monkeypatch, admitted_turn):
    monkeypatch.setattr("kimo_cli.telemetry.track", Mock(side_effect=RuntimeError("sink")))
    result = await admitted_turn.run()
    assert result.committed
```

- [ ] **Step 2: Run observability tests and verify RED**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_trigger_observability.py -q`

Expected: FAIL because Wiki trigger events and the privacy allowlist do not exist.

- [ ] **Step 3: Implement the safe event boundary and instrument each transition once**

```python
_EVENT_FIELDS = {
    "wiki_retrieval_hit": frozenset({"result_count", "injected_bytes", "duration_ms", "revision"}),
    "wiki_evidence_recorded": frozenset({"producer_role", "evidence_class", "reliable", "stable", "source_count"}),
    "wiki_checkpoint_created": frozenset({"cause", "producer_role", "evidence_count", "checkpoint_id"}),
    "wiki_checkpoint_resolved": frozenset({"outcome", "duration_ms", "checkpoint_id"}),
    "wiki_trigger_failed": frozenset({"stage", "error_class"}),
}

def track_wiki_event(event: WikiEventName, **fields: SafeScalar) -> None:
    allowed = _EVENT_FIELDS[event]
    safe = {key: value for key, value in fields.items() if key in allowed}
    try:
        track(event, **safe)
    except Exception:
        logger.debug("Wiki telemetry failed", exc_info=True)
```

Complete the allowlist for every specified event. Instrument only state-transition owners: retrieval owns hit/miss/skip; reporter owns evidence-recorded; coordinator owns created/resolved; Wiki tool owns candidate-discarded/approval-requested/committed; outer catch boundaries own trigger-failed. Do not emit a checkpoint event merely because evidence was observed. Emit exactly one terminal resolved outcome per checkpoint: `persist`, `discard`, `unresolved`, `cancelled`, or `unavailable`.

- [ ] **Step 4: Run observability and telemetry regression tests**

Run: `cd kimo-cli && uv run pytest tests/wiki/test_trigger_observability.py tests/telemetry tests/core/test_approval_telemetry.py -q`

Expected: PASS; event order matches runtime state, payload snapshots contain only allowlisted fields, and a telemetry/logger failure cannot alter retrieval, gate, Approval, or commit behavior.

- [ ] **Step 5: Commit**

```bash
git add src/kimo_cli/wiki/telemetry.py src/kimo_cli/wiki/retrieval.py src/kimo_cli/wiki/evidence.py src/kimo_cli/wiki/triggers.py src/kimo_cli/tools/wiki.py tests/wiki/test_trigger_observability.py
git commit -m "feat(wiki): observe deterministic trigger outcomes"
```

### Task 8: Lifecycle, Concurrency, Cross-Platform, and Full Runtime Integration

**Files:**
- Create: `kimo-cli/tests/core/test_wiki_trigger_lifecycle.py`
- Create: `kimo-cli/tests/wiki/test_trigger_cross_platform.py`
- Modify: `kimo-cli/src/kimo_cli/soul/agent.py`
- Modify: `kimo-cli/src/kimo_cli/soul/kimosoul.py`
- Modify: `kimo-cli/src/kimo_cli/background/manager.py`
- Modify: `kimo-cli/tests/core/test_cli_runtime_lifecycle.py`
- Modify: `kimo-cli/tests/core/test_shutdown_background_tasks.py`
- Modify: `kimo-cli/tests/core/test_kimosoul_steer.py`
- Modify: `kimo-cli/tests/core/test_kimosoul_background_wait.py`

**Interfaces:**
- Consumes: all Tasks 1–7 interfaces and existing idempotent `Runtime.close`/`WikiManager.close` behavior.
- Produces: complete fail-open/fail-closed runtime lifecycle with no end hook, no cross-session grant leakage, safe cancellation at every boundary, remote-KAOS read-only retrieval, and restart-safe background evidence delivery.

- [ ] **Step 1: Write failing cancellation, two-session, shutdown, no-end-hook, and platform tests**

```python
async def test_two_root_sessions_cannot_consume_each_others_checkpoint(runtime_factory):
    left = await runtime_factory(session_id="left")
    right = await runtime_factory(session_id="right")
    checkpoint = await create_admitted_checkpoint(left)
    result = await Wiki(right)(params_for(checkpoint))
    assert result.is_error
    assert left.wiki_coordinator.has_pending

@pytest.mark.parametrize("boundary", ["evidence", "delivery", "approval", "prepare", "commit"])
async def test_root_cancellation_fails_closed_at_every_boundary(boundary, scenario):
    runtime = await scenario.cancel_at(boundary)
    assert runtime.wiki_coordinator.unconsumed_grant_count == 0
    assert runtime.wiki.layout.revision.read_text() == "0\n"

async def test_session_end_compaction_archive_and_close_do_not_propose_or_commit(runtime):
    await runtime.close()
    await runtime.close()
    assert recorded_model_calls == []
    assert recorded_wiki_checkpoints == []
    assert runtime.wiki.layout.revision.read_text() == "0\n"

async def test_remote_kaos_retrieves_but_never_creates_workspace_file_evidence(remote_runtime):
    retrieval = await run_real_prompt(remote_runtime, "durable architecture")
    assert retrieval.result_count >= 0
    assert not any(e.source_class == "workspace-file" for e in remote_runtime.evidence)
```

- [ ] **Step 2: Run lifecycle/platform tests and verify RED**

Run: `cd kimo-cli && uv run pytest tests/core/test_wiki_trigger_lifecycle.py tests/wiki/test_trigger_cross_platform.py tests/core/test_cli_runtime_lifecycle.py tests/core/test_shutdown_background_tasks.py -q`

Expected: FAIL at uncovered cancellation, close, session isolation, and platform boundaries.

- [ ] **Step 3: Complete runtime ownership, cancellation, steer, and shutdown behavior**

Harden the root-only coordinator created in Task 2 and create its root reporter after Task 3 evidence wiring is available. Subagent runtime copies borrow only the reporter bridge described in Task 4. On Wiki initialization failure, leave coordinator/reporter unset, record one safe unavailable outcome, and let conversation continue. On root turn cancellation, call `cancel_turn(active_turn_id)` in `KimoSoul.run`'s `finally` before clearing turn state; propagate `CancelledError`. Steers extend the current real turn and may create one explicit durable checkpoint, but never rerun retrieval or reuse another turn's hashes. D-Mail revert, compaction, Stop-hook continuations, background notifications, and synthetic checkpoint reminders do not create real turns.

Make `Runtime.close` root-only and idempotent: close coordinator first (invalidating grants), then close `WikiManager` once, even if background reconciliation or hook cleanup raises. Shutdown does not resolve pending checkpoints by invoking the model or writing pages. Keep background manifests with their task records; cleanup/deletion follows the existing background task lifecycle.

- [ ] **Step 4: Add platform normalization and budget assertions**

Test Windows drive/UNC/backslash inputs through mocked Windows path semantics: evidence stores only registry-resolved relative POSIX paths. Test Linux/Docker configured shared Wiki root and remote KAOS. Verify no absolute path enters checkpoint blocks/manifests/events. Verify 512-byte query, 3-result/768-byte snippet/3-KiB retrieval, 4-checkpoint/8-evidence/1-KiB-summary/6-KiB block, and 16-unresolved caps with multi-byte Chinese at each boundary. Foreground backpressure waits for a batch to resolve; explicit durable intent remains first; background overflow stays sealed for later delivery.

- [ ] **Step 5: Run the complete Wiki/runtime/subagent regression suite**

Run: `cd kimo-cli && uv run pytest tests/wiki tests/tools/test_wiki_tool.py tests/tools/test_wiki_approval.py tests/tools/test_wiki_admission.py tests/tools/test_agent_tool.py tests/tools/test_background_tools.py tests/core/test_wiki_root_checkpoint.py tests/core/test_wiki_subagent_checkpoint.py tests/core/test_wiki_checkpoint_resolution.py tests/core/test_wiki_trigger_lifecycle.py tests/core/test_agent.py tests/core/test_toolset.py tests/core/test_runtime_roles.py tests/core/test_kimosoul_steer.py tests/core/test_kimosoul_background_wait.py tests/core/test_cli_runtime_lifecycle.py tests/core/test_shutdown_background_tasks.py tests/background -q`

Expected: PASS; qualifying root work works without subagents, qualifying subagent results checkpoint only after reaching root, explicit durable intent is deterministic, and no excluded session-end/Admin/import behavior appears.

- [ ] **Step 6: Run formatting, type, full backend, and outer packaging/privacy contracts**

Run: `cd kimo-cli && uv run ruff check src/kimo_cli/wiki src/kimo_cli/soul src/kimo_cli/subagents src/kimo_cli/background src/kimo_cli/tools/wiki.py && uv run ruff format --check src/kimo_cli/wiki src/kimo_cli/soul src/kimo_cli/subagents src/kimo_cli/background src/kimo_cli/tools/wiki.py && uv run pyright src/kimo_cli/wiki src/kimo_cli/soul/agent.py src/kimo_cli/soul/kimosoul.py src/kimo_cli/soul/toolset.py src/kimo_cli/subagents src/kimo_cli/background && uv run pytest tests -q`

Run: `cd .. && kimo-cli/.venv/bin/python -m pytest tests -q`

Expected: all commands PASS; outer privacy/package tests find no former private Wiki page, no Admin Panel change, no session-end archival hook, and no modification to `packaging/venvstacks.resolved.toml`.

- [ ] **Step 7: Inspect final scope and commit**

Run: `git status --short && git diff --check && git diff --name-only release/main...HEAD`

Expected: only planned runtime/Wiki/subagent/background/test/docs files are listed; `packaging/venvstacks.resolved.toml` is absent; no product Wiki page migration, Admin UI, embedding, daemon, watcher, or session-end hook exists.

```bash
git add src/kimo_cli/wiki src/kimo_cli/soul/agent.py src/kimo_cli/soul/kimosoul.py src/kimo_cli/soul/toolset.py src/kimo_cli/subagents src/kimo_cli/background tests/wiki tests/core/test_wiki_trigger_lifecycle.py tests/wiki/test_trigger_cross_platform.py tests/core/test_cli_runtime_lifecycle.py tests/core/test_shutdown_background_tasks.py tests/core/test_kimosoul_steer.py tests/core/test_kimosoul_background_wait.py
git commit -m "test(wiki): verify trigger lifecycle and platforms"
```

## Final Review Checklist

- [ ] Map every Scope, Non-goal, Design principle, Architecture, Retrieval, Checkpoint, Admission, Lifecycle, Privacy, Observability, Failure, Compatibility, and Testing requirement from `docs/superpowers/specs/2026-08-03-wiki-runtime-trigger-design.md` to at least one task above.
- [ ] Confirm built-in and custom subagents have no Wiki tool, prompt/index/retrieval block, checkpoint resolution, or grant capability; only their sealed evidence can reach the root.
- [ ] Confirm main-agent direct evidence work creates the same class of root-owned checkpoint even when no subagent is launched.
- [ ] Confirm an empty global Wiki makes automatic retrieval a no-op and does not add prompt text.
- [ ] Confirm exact raw/normalized user hashes, turn IDs, session provenance IDs, workspace IDs, run generations, source hashes, checkpoint IDs, and candidate hashes are consistently typed and checked at every later interface.
- [ ] Confirm model-supplied value/stability/reliability/source/checkpoint fields never create authority.
- [ ] Confirm duplicate result delivery, repeated completion, resume, notification plus `TaskOutput`, steer, cancellation, and concurrent writes cannot duplicate or replay checkpoints/grants.
- [ ] Confirm all byte limits preserve whole UTF-8 code points and all over-cap behavior is explicit backpressure/deferred background delivery rather than silent eviction.
- [ ] Confirm no raw prompt, summary, Shell data, credential, sensitive filename, absolute path, or page body appears in checkpoint records, manifests, logs, or telemetry.
- [ ] Confirm normal and AFK request Approval, YOLO only skips the popup, and all modes share the identical safety/value/source/novelty/transaction gate.
- [ ] Confirm session close, Runtime close, compaction, archive, and session end create no Wiki checkpoint, model call, proposal, or commit.
- [ ] Confirm every implementation step names concrete code, commands, and expected results rather than leaving deferred work.
