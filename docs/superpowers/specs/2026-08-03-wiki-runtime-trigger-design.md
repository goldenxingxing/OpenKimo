# Deterministic Global Wiki Runtime Triggers

## Goal

Make the existing global user Wiki participate reliably in normal OpenKimo use.
The runtime, rather than the model's initiative alone, must:

- retrieve a small amount of relevant global Wiki knowledge for each real root-user
  prompt;
- create a main-agent Wiki checkpoint after the root agent itself completes
  evidence-bearing work and forms a reusable conclusion;
- create a main-agent Wiki checkpoint after an evidence-bearing subagent result returns
  to the root agent;
- create a main-agent Wiki checkpoint when the user explicitly asks OpenKimo to
  remember something or states a durable preference, fixed rule, or cross-session fact;
- produce the trusted admission signals used by the value gate from runtime-observed
  events and source hashes, not from model-supplied booleans.

The result must keep the original product policy: the Wiki compounds only durable,
sourced, high-value knowledge. Making triggers reliable must not turn every tool call
or session into an automatic archive.

## Current behavior and diagnosis

The global Wiki introduced in `v0.1.26` is initialized correctly. On the inspected
installation it exists at the managed application-data location, has the generic
skeleton, and remains at global revision `0` with no content pages. Recent application
logs and telemetry contain no Wiki operation events.

The code explains this outcome:

- `Runtime._initialize_global_wiki` creates `WikiToolContext` with
  `candidate_high_value=False`, `stable=False`, and `reliable_source=False`.
- `KimoSoul.run` hashes real user text and recognizes a narrow explicit-remember
  phrase, but it does not schedule a Wiki action.
- the system prompt contains only advisory Wiki guidance and a compact index;
  nothing requires the root model to search, resolve, or discard a Wiki opportunity;
- there is no automatic search based on the current user prompt;
- the foreground and background subagent completion paths return summaries without
  creating a root-agent Wiki checkpoint;
- successful root-agent file, Shell, search, and fetch results are not converted into
  turn-bound Wiki evidence and therefore cannot trigger a root-only checkpoint;
- built-in subagents do not have the Wiki tool, which is desirable, but the root
  agent currently receives no trusted bridge from their evidence to its Wiki tool.

The existing gate can infer value and stability from an explicitly remembered turn or
independent source evidence, but those conditions are reached only after the model has
already chosen to call the Wiki tool. The missing piece is a deterministic runtime
trigger and a single-use, runtime-authenticated evidence grant.

## Scope

This change provides:

- bounded automatic global Wiki retrieval for real root-user prompts;
- a root-only, turn-scoped Wiki checkpoint coordinator;
- verified evidence capture for successful root and subagent tool results;
- deterministic checkpoint creation for qualifying root-agent work, qualifying
  subagent completions, and explicit durable user intent;
- a single-use runtime admission grant that binds value, stability, grounding, and
  source reliability to a turn, workspace, checkpoint, and source hashes;
- an explicit checkpoint discard path so medium- and low-value results disappear
  immediately;
- foreground and background subagent completion integration;
- privacy-safe observability for retrieval, checkpoint, gate, approval, and commit
  outcomes.

## Non-goals

This change does not:

- give any subagent the Wiki tool or permit a subagent to write the Wiki;
- automatically extract or write every subagent summary;
- archive, summarize, or inspect a session when it ends;
- migrate, scan, import, or package the 27 pages in the user's former workspace-local
  Wiki;
- add an Admin Panel page or other Wiki-management UI;
- add a pending-review candidate store;
- add embeddings, a daemon, a watcher, a second model call dedicated to extraction,
  or a separate Wiki runtime;
- relax secret, path, revision, approval, transaction, or recovery protections;
- treat an arbitrary successful Shell command or web search result as a reliable
  durable source by itself;
- create a checkpoint for ordinary chat, current progress/status, or an unsourced model
  hypothesis.

## Design principles

### Triggering and admission are separate

A trigger means only "the root agent must evaluate this opportunity." It does not
mean "write this to the Wiki." Glob, Grep, SearchWeb, and Shell can create a checkpoint
because they can produce useful evidence, but they cannot independently establish a
reliable durable source.

Admission remains stricter. A write needs a valid, single-use checkpoint plus source
references that match runtime-captured evidence. The existing safety, novelty,
duplicate, contradiction, approval, lock, revision, and transaction checks still run.

### Runtime signals are capabilities, not model claims

`candidate.value`, or any replacement classification submitted by the model, is not a
trusted admission signal. Runtime state produces an internal `WikiAdmissionGrant`.
The model cannot supply, serialize, or reconstruct that object.

The signals have precise meanings:

- `high_value`: this candidate is being evaluated within a runtime-created checkpoint
  caused by an explicit user directive or a qualifying evidence-backed result, and the
  root chose the persist branch of that checkpoint. It is not inferred from a
  `value="high"` parameter.
- `stable`: the grant has user authority or at least one current, exact, durable source
  snapshot. A search listing or transient command output alone is insufficient.
- `grounded`: every candidate and page source maps to an unconsumed evidence record
  held by the current runtime, with matching identifiers and content hashes.
- `reliable_source`: the referenced material was read or fetched successfully as exact
  content through an approved source class. It describes source integrity and binding,
  not an assertion that every statement in the source is universally true.

Semantic judgment still belongs to the main agent, but the model cannot manufacture
the authority or provenance needed to pass the gate merely by setting tool arguments.

### Root agent owns the decision

The root agent owns all three checkpoint flows. It can record a durable conclusion from
its own evidence-bearing work without using a subagent. Subagents remain focused workers:
they neither see nor call Wiki mutation operations, and their evidence becomes eligible
only after their result returns to the root. Explicit durable user instructions also
arrive at the root. In every case the root resolves the checkpoint by proposing a
candidate or discarding it.

## Architecture

### `WikiTurnCoordinator`

Add one root-runtime-owned `WikiTurnCoordinator`. It is an ordinary in-process module,
not a service. A subagent runtime may report evidence to its root coordinator but may
not resolve checkpoints or obtain admission grants.

The coordinator owns:

- the current root turn identifier;
- hashes of real user messages accepted by the prompt hook;
- verified evidence records from successful tool calls;
- active checkpoint records and their resolution state;
- consumed checkpoint and evidence digests for replay prevention;
- bounded automatic-retrieval results for the current turn;
- privacy-safe event emission.

Runtime creates a fresh turn state after `UserPromptSubmit` accepts the prompt and
before the first model step. Synthetic prompts, Stop-hook continuations, background
notifications, and subagent prompts do not masquerade as real user input.

The existing `Context.checkpoint` term is unrelated. Code and documentation use
`WikiCheckpoint` for this feature so the two concepts cannot be confused.

### Trusted records

The implementation uses internal frozen records conceptually equivalent to:

```python
@dataclass(frozen=True, slots=True)
class WikiEvidence:
    evidence_id: str
    root_turn_id: str
    session_provenance_id: UUID
    workspace_id: UUID | None
    producer_role: Literal["root", "subagent"]
    producer_id: str | None
    tool_call_id: str
    source_class: Literal[
        "workspace-file", "workspace-search", "shell-result",
        "web-search", "web-document", "workspace-mutation"
    ]
    request_hash: str
    result_hash: str
    logical_paths: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    reliable: bool
    stable_snapshot: bool

@dataclass(frozen=True, slots=True)
class WikiCheckpoint:
    checkpoint_id: str
    root_turn_id: str
    cause: Literal["root_evidence", "subagent_result", "explicit_user_durable"]
    evidence_ids: tuple[str, ...]
    summary_hash: str | None
    producer_id: str | None
    dedupe_key: str
    state: Literal["pending", "persisting", "discarded", "consumed", "cancelled"]

@dataclass(frozen=True, slots=True)
class WikiAdmissionGrant:
    checkpoint_id: str
    root_turn_id: str
    session_provenance_id: UUID
    workspace_id: UUID | None
    allowed_source_keys: frozenset[str]
    high_value: bool
    stable: bool
    grounded: bool
    reliable_source: bool
```

The implementation can split these records across focused modules, but the trust
boundaries and fields are normative.

Identifiers use random, unguessable values. Dedupe keys are SHA-256 digests over
canonical identifiers and hashes. Neither raw user text nor raw tool output is stored
in a checkpoint record.

### Tool-result evidence collector

`KimoToolset` reports a completed tool result to the coordinator only after the tool
finishes. A blocked, cancelled, timed-out, failed, empty, or parse-error result does not
create qualifying evidence.

The collector is role-aware and uses tool class identity plus normalized result
metadata; it does not trust a model-provided tool name or a field claiming success.
Evidence capture rules are:

| Tool outcome | May trigger checkpoint | Reliable/stable source by itself |
| --- | --- | --- |
| `ReadFile` / `ReadMediaFile` inside allowed workspace | yes | yes, after runtime hashes exact bytes and records UUID-relative path |
| successful `WriteFile` / `StrReplaceFile` | yes | yes, only after runtime re-reads and hashes the resulting allowed-workspace file |
| `Glob` / `Grep` with in-workspace matches | yes | no; root must read a specific matched artifact before write |
| successful non-empty `Shell`, exit code zero | yes | no; command/result hashes prove execution but not durable truth |
| `SearchWeb` with results | yes | no; a listing is discovery, not a reliable source |
| credential-free `FetchURL` with body | yes | yes for integrity, using normalized URL plus exact response hash |
| reasoning-only or no tool | no | no |
| any failed, denied, cancelled, or empty result | no | no |

For a file source, the runtime canonicalizes containment below the registered workspace,
converts it to a portable relative POSIX path, hashes the bytes, and records a normal
`workspace-file` `SourceRef`. Symlinks, unknown workspaces, absolute paths, and sensitive
files remain rejected by existing policy.

For Shell, only a hash of normalized invocation metadata and a hash of bounded result
bytes are retained. Raw commands, environment values, and output are not added to Wiki
metadata. Shell evidence can contribute to a root- or subagent-result checkpoint, but it
cannot grant `stable` or `reliable_source` by itself; the root must bind the reusable
conclusion to a cited durable artifact or discard it. Commands whose only outcome is
current status, progress, clock time, process state, or another explicitly transient
snapshot are marked non-triggering.

For web sources, URLs are normalized and checked for userinfo and sensitive query or
fragment parameters before recording. Search results trigger evaluation; only a
successful exact document fetch creates a reliable source record.

### Root-only tool boundary

The built-in coder, explore, and plan allowlists continue to omit `Wiki`. Their runtime
prompt also omits the managed global-Wiki index, per-turn retrieval block, and checkpoint
guidance; the root handles their results instead. In addition, the Wiki tool rejects
every mutation and checkpoint operation when
`runtime.role != "root"`, even if a future custom subagent specification accidentally
loads the tool. Subagent runtimes receive no `WikiToolContext` and no checkpoint
resolution capability.

They may share a read-only manager reference internally for startup efficiency only if
no model-facing read or write tool exposes it. The preferred implementation passes an
evidence reporter and leaves Wiki tool context unset on subagent runtime copies.

## Automatic retrieval

### Trigger point

After the user prompt hook accepts a real root-user message and before that message is
sent to the model, Runtime performs one bounded global Wiki search. It does not run for:

- slash commands;
- empty or whitespace-only input;
- synthetic internal prompts or background-task notifications;
- subagent prompts;
- input rejected by the user prompt hook;
- input containing detected credentials or sensitive tokens;
- an unavailable Wiki, an empty content catalog, or a normalized query shorter than
  two Unicode code points.

The normalized search query is built from at most 512 UTF-8 bytes of the real user text,
preserving whole code points. It is never logged verbatim.

### Result budget and injection

Search returns at most three results. The injected block is at most 3 KiB UTF-8 in
total, with at most 768 bytes of snippet per result. It contains only:

- logical Wiki path;
- title;
- one-line summary;
- bounded snippet;
- page revision and content hash.

Whole UTF-8 fields are preserved, and a truncation marker is included when needed. If
search returns no result, Runtime injects nothing. An empty Wiki therefore adds no
per-turn retrieval block.

The block uses managed markers such as
`OPENKIMO_WIKI_RETRIEVAL_START/END`. It is labeled as retrieved reference material,
not user instruction, and its text is excluded from conversation-source hashes and
explicit-remember detection. Retrieval failure is fail-open for the conversation:
Runtime records a safe event and continues without the block.

The existing startup compact-index block remains bounded at 8 KiB. The per-turn block
has its own 3 KiB limit; together they cannot grow with Wiki size or conversation
length.

### Retrieval evidence

A retrieval hit records the matched page revision and content hash in the turn ledger.
It may ground an answer. It does not automatically authorize rewriting the page. A
candidate based on retrieved Wiki material still needs a pending checkpoint and the
normal novelty/conflict checks.

## Deterministic checkpoint creation

There are exactly three co-equal checkpoint sources. All three create a checkpoint owned
and resolved by the root agent; none writes automatically.

### A. Evidence-bearing root-agent work

The main agent must be able to compound knowledge without launching a subagent. Root
tool calls report to the same turn-bound evidence collector. A `root_evidence`
checkpoint is created when all of the following are true:

1. the root is processing a real accepted user turn;
2. it has at least one qualifying successful workspace-file, Shell, search, fetched-web,
   or post-mutation evidence record;
3. it reaches a completion boundary with a non-empty conclusion intended to answer or
   complete the user's task; and
4. the evidence set is not solely transient status/progress and has not already produced
   an equivalent checkpoint in this turn.

The coordinator opens a root opportunity after the first qualifying result and
accumulates subsequent root evidence. At the first no-tool completion boundary, it seals
the evidence digest, creates the checkpoint, and requires the root to persist or discard
before `TurnEnd`. This does not expose an unfinished conclusion to the Wiki, create a
second model extraction job, or wait for session shutdown.

Examples that can create a root checkpoint include reading and reconciling project
configuration, diagnosing a recurring failure from logs plus source, fetching a durable
external reference, and completing an evidence-backed workflow whose reusable conclusion
will help future sessions. The Runtime binds the checkpoint to the root turn, workspace
UUID, tool-call IDs, source hashes, and conclusion hash.

These turn-bound root signals enter the identical write policy as subagent evidence:
normal and AFK modes require `wiki.write` Approval, while YOLO may skip only the popup
after the same value, stability, grounding, reliability, novelty, and safety gate passes.

Pure chat, ungrounded reasoning, a model assertion that its answer is important, a
current `git status`, task progress, a one-off test count, clock/process state, or an
unsourced hypothesis does not create a root checkpoint. Root `Shell` or search evidence
that may contain a reusable conclusion can open an opportunity, but it needs an exact
workspace-file or fetched-document source before it can receive stable/reliable admission.

If the root has already received a `subagent_result` checkpoint and then reads the exact
source to resolve it, the new root evidence augments that existing checkpoint rather than
creating a duplicate `root_evidence` checkpoint for the same conclusion and source
digest.

### B. Evidence-bearing subagent result returned to the root

#### Foreground result

Each subagent run has an evidence accumulator keyed by root runtime, agent ID, and run
generation. When a foreground run completes successfully and produces a final summary,
the runner seals its evidence before constructing the `Agent` tool result.

A `subagent_result` checkpoint is created only when:

1. the run completed successfully and was not cancelled or timed out;
2. at least one qualifying successful evidence record exists;
3. the final summary is non-empty; and
4. the root runtime that launched the run is still available.

The checkpoint binds the summary hash and evidence digest. The normal Agent result still
contains the summary; a compact managed checkpoint block is appended after it for the
root model. The block lists cause, checkpoint ID, source counts, portable source refs,
and the required resolution action. It never includes absolute paths, raw Shell output,
credentials, or an extra copy of the full summary.

A subagent that only reasons, asks a question, fails, or invokes no qualifying evidence
tool returns normally with no checkpoint.

#### Background result

Background completion uses the same sealed evidence record. The first terminal
completion notification or completed `TaskOutput` result observed by the root attaches
the checkpoint block. Reconciliation after restart may reconstruct it from a small
persisted subagent evidence manifest containing only hashes, portable paths, tool class,
status, and run generation.

The evidence manifest is not a Wiki candidate, review queue, or source-content archive.
It follows the subagent execution record's lifecycle. Raw tool output remains in its
existing execution artifacts and is not duplicated.

Failed, killed, lost, or timed-out background tasks create no checkpoint. Delivery
through both notification and `TaskOutput` is deduplicated.

### C. Explicit durable user intent

After prompt-hook acceptance, a multilingual, negation-aware detector evaluates only
raw user-authored text. It recognizes three narrow durable-intent families:

- direct remember requests, such as "remember this" or "写入知识库";
- explicit long-term preferences, such as "from now on, I prefer..." or "以后都...";
- explicit fixed rules or cross-session facts, such as "our permanent release rule is..."
  or "后续所有工作区都要遵守...".

A positive match deterministically creates one `explicit_user_durable` checkpoint before
the first model step. It binds:

- root turn ID and session provenance ID;
- current workspace UUID, when local;
- SHA-256 hashes of the exact raw and normalized user text;
- the matched durable-intent family.

The detector uses bounded, reviewed language patterns and explicit durability markers;
it does not ask the model whether arbitrary chat is a preference or fact. Negated forms
such as "do not remember", "only for this run", "不要记住", and "这次临时使用" create no
checkpoint. A statement limited to the present task, current status, or transient value
does not match. Repeated durable phrases in one accepted prompt create one coalesced
checkpoint.

Explicit durable user intent supplies user authority for high value and stability, but
does not bypass secret checks, source matching, duplicate detection, approval, revision,
or transaction checks.

### Dedupe, concurrency, and budget

The checkpoint dedupe key includes cause, session provenance ID, root turn, producer/run
generation when present, conclusion or summary hash, and ordered evidence IDs. Replaying
an Agent result, re-observing the same root result, reconciling the same background task,
reading `TaskOutput` repeatedly, or resuming the root UI cannot create a second
checkpoint for the same conclusion.

Parallel subagent completions may create separate records, but pending records delivered
before one model step are coalesced into one managed block. A block includes at most four
checkpoints, eight evidence refs per checkpoint, 1 KiB of summary context per checkpoint,
and 6 KiB total. The coordinator holds at most 16 unresolved checkpoint records in
memory. Additional background completions remain in their existing bounded evidence
manifests and are delivered in later batches; additional foreground results apply
backpressure until one batch is resolved. Qualifying results are not silently evicted.
An `explicit_user_durable` checkpoint is delivered first and is never displaced by a
root- or subagent-evidence checkpoint.

This bounded ephemeral checkpoint state is distinct from the forbidden pending-review
candidate store. No candidate Markdown survives a discard, decline, cancellation, or
process exit.

## Main-agent checkpoint resolution

### Resolution protocol

Wiki write operations gain a required `checkpoint_id`. The root has exactly two ways to
resolve a pending checkpoint:

1. `persist`: call `Wiki remember` or `Wiki ingest` with a structured candidate and the
   checkpoint ID;
2. `discard`: call a lightweight root-only Wiki checkpoint-discard operation with the
   checkpoint ID and a bounded reason code (`low_value`, `unstable`, `ungrounded`,
   `duplicate`, or `not_reusable`).

Discard stores no candidate and writes no Wiki file. It consumes the checkpoint and
emits only a privacy-safe event.

When a checkpoint is delivered, Runtime marks it unresolved. If the model attempts to
finish the root turn without resolving it, `KimoSoul` injects one synthetic internal
follow-up reminding the main agent to persist or discard. This is the deterministic
checkpoint guarantee: the opportunity cannot vanish silently after qualifying root
work, a subagent result, or explicit durable user intent.

There is no infinite loop. If the root still does not resolve the checkpoint after one
continuation, Runtime discards it with reason `unresolved`, records the outcome, and lets
the response finish. Wiki unavailability also resolves it as `unavailable` without
blocking the user.

Synthetic checkpoint prompts are not user-confirmed input, do not run automatic
retrieval, and do not add conversation hashes.

### Single-use admission grant

When the root submits a candidate, Runtime atomically transitions the checkpoint from
`pending` to `persisting` and computes a `WikiAdmissionGrant` from the coordinator's
ledger. A grant is issued only when:

- checkpoint/session/turn bindings match the active root runtime;
- the checkpoint is pending and has not been consumed, cancelled, or replayed;
- every source in the candidate and every page maps to an allowed evidence record;
- workspace IDs, UUID-relative paths, normalized URLs, and content hashes match;
- exact durable sources still match their captured hash at prepare time;
- at least one eligible source establishes grounding;
- stability and reliable-source rules below are met.

Rules by checkpoint cause:

- `root_evidence`: runtime sets high-value authority only for the pending root completion
  checkpoint and its exact conclusion/evidence digest. Stability requires at least one
  exact durable source snapshot from this root turn. Shell and search-list evidence alone
  cannot satisfy stable or reliable admission.
- `subagent_result`: runtime sets high-value authority only for this pending,
  evidence-bearing checkpoint. Stability requires at least one exact durable source
  snapshot from that run or a root follow-up tool call in the same turn. Reliability
  requires all sources used for admission to be exact workspace-file or fetched-document
  evidence. Search listings and Shell-result hashes alone cannot satisfy the write gate.
- `explicit_user_durable`: runtime sets high-value and stable authority from the real
  user directive. Conversation sources must match the current session provenance ID and
  exact accepted user-text hash. Other source kinds still require ledger matches.

The model's `candidate.value` field is ignored for authorization and may be removed in a
schema migration. Existing callers may continue to send it temporarily, but changing it
cannot affect the grant.

If a file changes after evidence capture, preparation fails as stale grounding. The root
may read it again to establish a new evidence record; Runtime never silently accepts the
old hash.

### Gate and commit order

The write path remains:

1. authenticate and reserve the checkpoint;
2. construct trusted gate context from the admission grant;
3. validate source, secret, path, page, novelty, duplicate, and conflict policy;
4. discard immediately if medium/low, unstable, ungrounded, unsafe, or duplicate;
5. in normal or AFK mode, request compact `wiki.write` Approval;
6. in YOLO mode only, skip the popup but keep the identical gate;
7. acquire the writer lock, revalidate revision/source state, and commit atomically;
8. consume the checkpoint and evidence grant.

Approval is still requested before acquiring the writer lock. A declined, cancelled, or
undeliverable Approval consumes and discards the candidate. It creates no pending item.
If commit returns a retryable revision conflict, the grant may be retried once within the
same active turn after revalidation; it cannot be reused for different content.

AFK is not YOLO. AFK Wiki writes still create and wait for page Approval.

## Low-value and stability policy

Runtime checkpoint creation is intentionally narrower than "the root used a tool" or
"a subagent returned":

- no checkpoint for root work that contains no qualifying evidence or reusable
  conclusion;
- no evidence checkpoint for pure prose or reasoning without tool evidence; an explicit
  durable user-intent checkpoint is the sole conversation-only exception;
- no checkpoint for failed or empty tools;
- no checkpoint for status-only background notifications;
- no checkpoint merely because a model labels its answer important;
- no stable admission from live Shell state, a file listing, a search result list, or a
  transient timestamp;
- no checkpoint for ordinary chat, temporary preferences limited to the current task, or
  durable intent that is negated explicitly;
- no write from a checkpoint whose sources are unavailable, changed, or sensitive;
- duplicate material is discarded before Approval;
- one-off build output, temporary paths, current task progress, failed attempts, and
  ordinary conversation summaries are classified as non-reusable and discarded.

Good candidates include durable architecture facts, confirmed user preferences,
repeatable workflows, resolved recurring failure causes, stable project constraints,
and sourced reference knowledge. The main agent must express only the durable conclusion,
not the subagent's execution diary.

## Lifecycle and cancellation

- A root turn cancellation cancels unresolved `root_evidence` and
  `explicit_user_durable` checkpoints created by that turn and consumes no evidence
  grant.
- Cancelling a foreground subagent before success creates no checkpoint.
- A background subagent may complete after its launching turn. Its sealed evidence is
  attached when the root next receives the completion result; the new checkpoint is
  resolved in that receiving root turn while retaining the original evidence hashes.
- Closing a session does not run retrieval, create a checkpoint, invoke the model, or
  write Wiki content.
- Runtime shutdown invalidates unconsumed in-memory grants. Evidence manifests used for
  background completion recovery contain no candidate and cannot authorize a write
  without a fresh root checkpoint.
- `Runtime.close` retains the existing idempotent WikiManager shutdown behavior.

Coordinator mutation is protected by an asyncio lock on the owning root loop. Worker
threads return immutable observations through loop-safe callbacks. No process-global
`ContextVar` is used as the sole checkpoint store; concurrent sessions and parallel tool
calls cannot observe one another's grants.

## Privacy and security

Checkpoint and telemetry payloads may contain:

- random turn/checkpoint/evidence IDs;
- workspace UUID;
- UUID-relative logical source paths that already passed sensitive-file policy;
- credential-free normalized origin/URL where required for an approved source;
- SHA-256 hashes;
- counts, durations, operation names, and bounded reason codes.

They must not contain:

- machine absolute paths;
- raw Shell command text, environment variables, or output;
- raw user prompts, raw subagent prompts, or full summaries;
- API keys, cookies, authorization headers, URL credentials, or sensitive filenames;
- unrestricted file lists or retrieved page bodies.

Path resolution uses the existing workspace registry and containment checks. A model
cannot turn a Shell result into a workspace file source by inventing a path; Runtime must
open, contain-check, and hash the file itself. Evidence from one workspace, session,
user namespace, or root runtime cannot authorize a candidate in another.

## Observability

Emit structured events through the existing telemetry/logger boundary:

- `wiki_retrieval_hit`: result count, injected bytes, duration, revision;
- `wiki_retrieval_miss`: duration and skip-safe category;
- `wiki_retrieval_skipped`: unavailable, empty, synthetic, sensitive, slash command,
  or too short;
- `wiki_evidence_recorded`: producer role, evidence class, reliable/stable flags, and
  bounded source count;
- `wiki_checkpoint_created`: cause, producer role/type, evidence classes/count, and
  checkpoint ID;
- `wiki_checkpoint_resolved`: persist/discard/unresolved/cancelled and duration;
- `wiki_candidate_discarded`: bounded gate reason;
- `wiki_approval_requested`: page count and execution mode;
- `wiki_committed`: page count, global revision, and search-index-current flag;
- `wiki_trigger_failed`: safe stage and exception class only.

Events never include the search query, page body, summary text, absolute path, raw tool
arguments/output, or credentials. Logging/telemetry failure never changes gate or commit
behavior.

The event flow is deterministic:

1. an accepted real root prompt emits one retrieval hit/miss/skip; qualifying durable
   user intent additionally emits `wiki_checkpoint_created(cause=explicit_user_durable)`;
2. each qualifying root tool result emits `wiki_evidence_recorded(producer=root)`, and
   the reusable root completion boundary emits
   `wiki_checkpoint_created(cause=root_evidence)`;
3. each qualifying subagent tool result emits
   `wiki_evidence_recorded(producer=subagent)`, and successful result delivery to the
   root emits `wiki_checkpoint_created(cause=subagent_result)` once;
4. every checkpoint emits exactly one terminal `wiki_checkpoint_resolved`; persist then
   proceeds through candidate-discarded or approval-requested and finally committed or a
   safe failure event.

A source-observation event does not mean a checkpoint was created, and a checkpoint event
does not mean a write was admitted.

## Failure behavior

- Retrieval failure: continue the user turn without retrieval context.
- Evidence normalization failure: omit that evidence and log a safe reason; do not fail
  the subagent result.
- Checkpoint block injection failure: return the normal subagent result or finish the
  normal root response and emit a safe trigger-failed event; the task itself is not
  converted to failed.
- Wiki unavailable: resolve checkpoint as unavailable and continue.
- Forged, expired, cross-turn, cross-session, or replayed checkpoint: reject the write.
- Changed source hash: reject as stale grounding and require a fresh read.
- Approval failure or decline: fail closed and discard.
- Root cancellation: invalidate grants and propagate cancellation.
- Writer busy or revision conflict: preserve existing retryable behavior without
  widening the grant to other content.

## Compatibility

Markdown remains authoritative and the global Wiki path, FTS cache, WAL policy,
transaction journal, compact startup index, and Approval UI contract remain unchanged.
No existing Wiki page is rewritten merely by installing this change.

The current single-user namespace remains `users/default`. Future login work must give
each authenticated user a separate coordinator, evidence ledger, and Wiki namespace;
checkpoint IDs and evidence grants must never cross user identities.

## Testing strategy

### Retrieval

- a real English or Chinese root prompt retrieves at most three matching global pages;
- empty Wiki and zero-result search inject no block;
- slash, synthetic, subagent, rejected-hook, secret-bearing, empty, and too-short input
  skip retrieval;
- UTF-8 query, field, result, and total byte limits preserve whole code points;
- retrieval markers cannot be duplicated across continuations;
- FTS failure uses existing bounded fallback and never blocks the turn;
- retrieval telemetry contains no query or body.

### Explicit durable user intent

- English and Chinese positive forms create exactly one checkpoint;
- negated forms create none;
- explicit long-term preferences, fixed rules, and cross-session facts match, while
  current-task-only and temporary forms do not;
- raw and normalized text hashes bind conversation sources;
- model-supplied session ID, content hash, high value, stability, or checkpoint ID cannot
  forge a grant;
- an unresolved checkpoint gets one internal continuation and then a deterministic
  discard rather than a loop.

### Root-agent bridge

- root ReadFile, post-write file re-read, Shell, Grep/Glob, SearchWeb, and FetchURL
  outcomes produce the same typed, turn-bound evidence records as subagent work;
- a root that completes reusable evidence-backed work without launching any subagent
  receives one `root_evidence` checkpoint;
- a root no-tool reasoning answer, ordinary chat, failed tool, empty output, temporary
  status, one-off progress result, or unsourced hypothesis receives none;
- a root Shell/search conclusion can trigger evaluation but cannot pass stable/reliable
  admission without an exact workspace-file or fetched-document source;
- repeated no-tool completion attempts and repeated tool results do not duplicate the
  checkpoint;
- root follow-up evidence used to resolve a subagent checkpoint augments that checkpoint
  instead of creating an equivalent root checkpoint;
- forged model `high`, `stable`, source hashes, or checkpoint IDs cannot replace the
  runtime's root-turn evidence grant.

### Subagent bridge

- built-in coder/explore/plan do not expose Wiki;
- a custom subagent attempting to load Wiki cannot resolve or mutate it;
- successful ReadFile, post-write file re-read, Grep/Glob, Shell, SearchWeb, and FetchURL
  produce the correct evidence classes;
- no-tool, failed, empty, denied, cancelled, timed-out, lost, and killed runs create no
  checkpoint;
- successful evidence-bearing foreground completion creates one root checkpoint after
  final summary and before Agent tool return;
- background notification and repeated TaskOutput produce one deduplicated checkpoint;
- resumed agent runs use a new run generation and cannot replay old evidence;
- eligible non-transient Shell/search evidence can trigger a checkpoint but cannot pass
  stable/reliable admission until the root obtains an exact durable source;
- source file mutation after evidence capture invalidates the grant.

### Gate and approval

- candidate `value="high"` without a valid checkpoint is rejected;
- valid root-evidence and explicit-user-durable grants reach normal/AFK Approval only
  after their distinct source rules pass;
- a valid sourced subagent checkpoint reaches Approval only after all source hashes match;
- normal allow-once, session-wide allow, decline, cancellation, and delivery failure keep
  existing behavior;
- AFK still requests Approval;
- YOLO skips only Approval and passes the identical gate;
- low-value, unstable, ungrounded, sensitive, duplicate, and unresolved candidates are
  discarded with no candidate file or hidden queue;
- a checkpoint and its grant are single-use under concurrent calls.

### Concurrency and lifecycle

- concurrent root tool calls merge into one cause-bound turn ledger without losing or
  cross-binding source hashes;
- parallel subagent results create isolated, bounded, deduplicated checkpoint records;
- two root sessions cannot consume one another's checkpoint or evidence;
- cancellation at evidence capture, checkpoint delivery, approval, prepare, and commit
  boundaries fails closed;
- background completion after the launching turn is delivered safely in the next root
  receiving turn;
- restart recovery reconstructs only safe background evidence metadata, never a Wiki
  candidate;
- session end, compaction, archive, and Runtime close produce no automatic Wiki proposal,
  model call, or commit.

### Privacy and regression

- checkpoint blocks, evidence manifests, telemetry, and logs contain no absolute paths,
  raw prompts, commands, outputs, secrets, or credential-bearing URLs;
- source paths remain workspace UUID plus relative POSIX path;
- all existing Wiki initialization, search, schema, transaction, recovery, approval,
  Knowledge API, Docker/KAOS, and package tests continue to pass;
- repository and package inspection confirms that no former private Wiki pages are added;
- Admin Panel source and behavior remain unchanged.

## Acceptance criteria

- A normal real-user question receives a bounded relevant global Wiki block when matches
  exist and no block when the Wiki is empty.
- Evidence-bearing root work can produce durable Wiki knowledge without any subagent,
  and cannot silently skip its one root checkpoint resolution attempt after a reusable
  conclusion is completed.
- Every evidence-bearing subagent completion is evaluated once by the root agent; a
  reasoning-only or failed subagent completion is not.
- A real explicit remember request, long-term preference, fixed rule, or cross-session
  fact cannot silently finish without one root checkpoint resolution attempt.
- Ordinary chat, temporary state/progress, current-task-only preference, and unsourced
  speculation create no checkpoint.
- Subagents have no Wiki tool and cannot write or resolve Wiki checkpoints.
- Runtime-generated single-use grants, not model fields, supply high-value, stable,
  grounding, and reliable-source authority.
- Root and subagent Shell/search evidence follow the same turn-bound trust rules: they
  can trigger evaluation but cannot independently authorize durable knowledge.
- Normal and AFK modes use compact Approval; YOLO alone skips the popup and still uses
  the same gate.
- Medium/low, unstable, ungrounded, duplicate, declined, cancelled, and unresolved
  candidates are discarded without pending storage.
- No session-end archive, old-page migration, Admin Panel work, embeddings, or daemon is
  introduced.
- All prompt, evidence, concurrency, cancellation, dedupe, privacy, path, byte-budget,
  approval, and regression tests described above pass.
