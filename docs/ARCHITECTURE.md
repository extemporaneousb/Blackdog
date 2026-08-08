# Architecture

Blackdog is organized around one durable idea: the machine-owned workset store
is the semantic source of truth.

Humans author repository docs, design docs, approvals, and prompts.
Agents mutate planning and runtime state through typed Blackdog operations and
CLI surfaces. Humans can inspect the resulting files, but they are not the
preferred authoring plane.

This document owns package boundaries, storage ownership, repo lifecycle
layering, and shipped workflow ownership. CLI syntax belongs in
[docs/CLI.md](docs/CLI.md); JSON/TOML/event schemas belong in
[docs/FILE_FORMATS.md](docs/FILE_FORMATS.md).

## Package Boundaries

| Package | Role | Must not absorb |
| --- | --- | --- |
| `blackdog_core` | Durable planning/runtime contracts, typed models, and derived read models. | CLI glue, orchestration policy, HTML/view composition, or prompt-only behavior. |
| `blackdog` | Product-layer WTAM orchestration and repo lifecycle workflows on top of the core contract. | Canonical planning or runtime storage ownership. |
| `blackdog_cli` | Thin parser/help/dispatch layer behind the `blackdog` executable. | Domain logic or storage semantics. |

The hard rule is unchanged: `blackdog_core` defines the contract and every
other layer consumes it.

## Durable Contract

The durable contract under the control root is:

- `planning.json`
- `runtime.json`
- `events.jsonl`

`planning.json` owns the durable workset/task DAG.
`runtime.json` owns mutable task execution state, including workset claims,
task claims, prompt receipts, and worktree/git lineage for attempts.
`events.jsonl` records append-only mutations for audit and inspection.

`backlog.md` is not a storage dependency anymore.
Markdown fence parsing, raw text surgery, and plan-block compatibility logic are
gone from the semantic write path.

## Core Model

The top-level durable planning object is `Workset`.

A workset owns:

- scope
- task DAG
- visibility boundary
- policies
- canonical exported workspace identity
- branch intent for target and integration branches

Tasks remain the executable unit inside a workset, but they are no longer
grouped durably by `epic`, `lane`, or `wave`. Those concepts were structurally
wrong for the AI-first target model and were removed instead of preserved as
aliases.

Claims attach to both worksets and tasks. The shipped write path exposes one
kept-change execution model:

- `direct_wtam` for one kept-change task running through the WTAM lifecycle

Older runtime files may still load one removed managed-claim token during
migration, but that token is not part of the active runtime contract.

## Storage Boundary

`blackdog_core.backlog` exposes a planning-store interface rather than baking
JSON file operations into every semantic function. The shipped implementation is
`JsonPlanningStore`, but the semantic layer works on typed worksets and tasks.

`blackdog_core.state` does the same for runtime state through a JSON-backed
runtime store. That keeps storage substitutable without reintroducing text-based
plan editing.

## User-Local State

Blackdog also has user-local operator/read-model state outside checked-in repo
state and outside each repo's control root. `BLACKDOG_HOME` is the explicit
override. Without it, Blackdog uses `$CODEX_HOME/blackdog` when `CODEX_HOME` is
set, otherwise `~/.codex/blackdog`.

Current user-local files are:

- `local-repos.json` for explicit local repo registry membership used by
  cross-repo read models only when registry scope is selected (bare `stats`
  retains that selection as a reported compatibility fallback)
- `codex/session-cache-v1.json` for parsed Codex session metadata used by
  Codex coverage, history, repo table, and stats read models

This state is not planning truth, runtime truth, or a repo membership authority
for scanned commands. It is cache and operator convenience state. The durable
repo contract remains `blackdog.toml` plus the control-root planning/runtime
files.

Fleet read models share one product-layer repository-scope contract: exact
project roots, read-only discovery beneath explicit roots, or explicit registry
selection. Discovery and registry selection are mutually exclusive, deduplicate
resolved aliases, and report bounded selection/error evidence without mutating
repository or registry state. These reporting paths load profiles read-only;
they neither create a missing control directory nor prune Git worktrees.
One shared second-stage profile resolution owns canonical roots, alias
deduplication, and bounded profile-load errors for both repo table and stats.
Fleet discovery and registry reads therefore degrade per candidate, while
exact project-root reads remain strict.

## Workflow Families

Blackdog has two product-layer workflow families:

1. workset execution workflows over typed planning/runtime state
   (`workset`, `summary`, `next`, `task`, and `worktree`)
2. repo lifecycle/operator-read workflows over repo analyze/bind/table/
   scaffold/install/update/refresh/archive/unarchive/unbind, prompt/skill
   composition, attempt inspection, user-local registry management, and stats
   reporting

The second family is intentionally not part of the workset/task durable model.
Analyze/bind/table/scaffold/install/update/refresh/archive/unarchive/unbind,
prompt preview/tune, attempts summary/table, local-repo registry commands, and
stats are product workflows, but they are not claims, tasks, or attempts.

Prompt-bearing commands share one product-layer input contract. Request
composition uses `--request`/`--request-file`, execution uses
`--execution-prompt`/`--execution-prompt-file`, and `task begin` may record a
separate request lineage. Former prompt and user-prompt spellings are supported
parser aliases, not a second semantic path; both spellings converge before
receipt creation and preserve existing hashes, source labels, and provenance.
The normal CLI actor defaults to the stable `codex` owner. In supervised work,
one explicit `codex-supervisor` owns the task and attempt; workers do not open
parallel Blackdog attempts. Post-parse setup or managed-skill refusal happens
before task mutation and returns a blocked `task.begin` operation result with
null task/attempt state and one bounded required-input action. The result still
passes through fail-open lifecycle observability without changing its outcome.

Any future orchestration beyond the direct WTAM path still belongs in
`blackdog`, not in `blackdog_core`. The core model should stay small while the
product layer owns higher-level operator policy.

Normal task lifecycle ergonomics follow the same boundary. `blackdog` owns the
typed operation result, deterministic next-action decision matrix, executable
argv construction, mutation-phase reporting, and typed Git/worktree exception
classes. `blackdog_cli` only parses, dispatches, chooses JSON or text rendering,
preserves established wrapper keys, and maps typed completion to process exit
status. `blackdog_core`
continues to own durable task/attempt records and the bounded failure-class
values written to runtime state; it does not absorb shell commands, recovery
prose, WTAM orchestration, or message-based exception classification.

One post-operation state produces exactly one primary `next_action`. Commands
and bounded choices carry complete argv; complete and blocked states carry none.
For every structured result from task begin, show, recover, cancel, reopen,
land, reconcile-landing, close, or cleanup, this action is the sole authority
regardless of `operation_status`: an agent executes exact argv or one complete
bounded choice or alternative, stops for blocked or complete actions, and never
derives an action from diagnostic prose or compatibility recommendations.

Landing evidence is an agent-owned input boundary. Dirty or branch-ahead active
tasks expose a blocked `landing_evidence_required` action until the caller
provides a nonblank completion summary and at least one explicit validation
row. Blackdog does not fabricate closeout evidence, and an incomplete first
landing request stops before transaction, event, runtime, or Git mutation.
Normal task text places this authoritative operation/action block before
diagnostics and omits deprecated recommendation views that remain in JSON.

The decision model treats same-envelope resume as a new attempt in the existing
workset/task, not as task creation. The product layer atomically persists the
normalized request and execution text in private, bounded, immutable SHA-256
addresses under the shared control root before a new attempt is reserved; the
core receipt stores only their additive relative replay paths. Resume is
executable only when persisted actor attribution, prompt hashes/modes, and the
exact artifact bytes are verified. Original source files are audit provenance,
not a replay dependency. Historical receipts without an artifact path retain
their verified source-file fallback. A recorded artifact that is missing or
invalid blocks as lineage-required and is never reconstructed from that
fallback. The emitted command carries the expected actor and both expected
prompt lineages, and `task begin` repeats that comparison at the mutation
boundary even if a caller omits those expected-value flags; no prompt is
synthesized from stale state. Agent-owned temporary request and execution input
files are disposable only when the structured begin result contains both a
nonempty `execution_prompt_replay_artifact_path` and a nonempty
`user_prompt_replay_artifact_path`; otherwise both inputs remain recovery
evidence. Active-attempt land/close attribution belongs to
the attempt actor. Explicit cancel/reopen transitions require and durably record
their invoking actor, which owns subsequent task-state attribution. Canceled
tasks must reopen first. A dirty or unproven retained workspace is inspected before
cleanup, and missing metadata, refs, or reference-inspection proof block repair
instead of being mistaken for a landable branch. Landing failures
are classified by product-layer exception type; arbitrary legacy
`WorktreeError` prose remains `unknown`.

Ordinary resume uses the same atomic successor-reservation boundary as retained
workspace adoption. Product preflight captures the exact latest predecessor,
restartable task-state generation and durable owner, and both prompt lineages,
then performs worktree and handler setup. The core rechecks that guard while
holding the runtime mutation lock and derives the ordinary successor id from
the envelope, predecessor, actor, and prompt lineage. The resulting
`setup_receipt.atomic_start` is durable proof of that decision. Exact retries
reuse the same attempt, claims, and start events. Claim ownership is derived
again from canonical runtime timestamps and note rather than trusted from the
receipt. A stale guard creates no successor state, and the product removes the
worktree and branch it created before the conflict was detected.

Resume-cycle attribution uses ledger order, not timestamp precision. The one
exact predecessor `task.finish` row bounds the cycle; only matching
cancel/reopen transitions appended after it may replace the predecessor actor
and generation. A duplicate finish boundary is ambiguous proof and returns
typed blocked `task_start_proof_required` before prompt persistence, Git
preview, runtime reservation, handlers, or event writes. Only a historical
ledger with no exact finish boundary uses the bounded timestamp fallback.

Initial and ordinary-resume starts are recoverable transactions after runtime
reservation. The attempt stores an immutable `worktree_start` receipt binding
the requested base ref, resolved base commit, and canonical primary-worktree
path. Core `workset.claim` (when this attempt created it), `task.claim`, and
`task.start` rows and the product `worktree.start` row have deterministic,
attempt-scoped identities. The product event is built from the durable setup
receipt's canonical handler projection, so execution timing or a later
`created` versus `validated` observation cannot change retry identity.

Repair holds the same attempt lifecycle lock used by close, cancel, landing,
and cleanup. Before appending a missing event or recreating a workspace it runs
a read-only identity preflight over recorded canonical paths, Git worktree
registration, branch and HEAD commit, clean status, and the live handler plan.
Handler action order is nonsemantic but multiplicity, handler id/kind,
normalized action, target, status, and summary paths must agree with the durable
receipt. A missing owned workspace may be recreated at the exact recorded start
commit; a moved ref, alternate registration, path alias, changed handler
contract, or conflicting deterministic event blocks without changing runtime,
events, handler outputs, refs, or worktrees. Land, close, and cancel cannot
terminalize an attempt until this evidence is complete.

A crash after any reservation boundary returns a typed partial `task.begin`
result with the retained workset/task/attempt identity and the state-derived
`next_action`. If only the new planning envelope and private prompt artifacts
were retained, that action retries the same envelope; if an attempt was
reserved with missing evidence, it is `repair_task_start_evidence`. Exact
completed retries do not replace `runtime.json`, append event bytes, or create
a second workspace. Concurrent retries serialize on the attempt lock and
converge on one attempt, one claim set, and one event per deterministic id.

Task-state actor and attempt actor have deliberately different meanings. The
task-state actor is the durable current owner of a restartable envelope and is
updated by cancel/reopen transitions. An attempt actor is immutable historical
attribution for that execution. Legacy task-state rows without an actor remain
readable; product lineage resolution may use their latest task-state event or
predecessor attempt, while every new mutation persists the resolved owner.

Stale-claim release is a core-owned, deterministic two-store transaction for
the narrow state where a claim survives without an active attempt. Its request
freezes the exact task, claim, terminal-attempt slice, failure semantics, and
pre-claim set. Under the runtime lock, its decision freezes the pre/post task
and workset projections plus the deterministic owned `task.release` and
conditional `workset.release` rows. Request and decision precede runtime
replacement; owned events follow it before the lock is released. Exact guarded
retries can therefore converge every interrupted stage without treating an
unknown post-save failure as a failed release, and conflicting or superseded
evidence cannot be silently adopted.

The pending stale-release read model is workset-scoped because its decision
owns the complete claim-set projection. The product layer translates that
owner identity into one exact retry action and gates claim-mutating begin,
land, and close before workspace or Git effects. Read-only sibling surfaces
point to the same owner action. The internal request/decision guards are
machine-emitted replay capabilities, hidden from ordinary grammar and never
agent-authored. Task-state-only mutation for a different task remains
available; mutation of the owner or claim topology waits for exact repair.

Planning membership has one separate owner boundary. Task-scoped core
mutators re-read planning at their runtime linearization point, reject a target
removed by a winning planning update, and merge against every sibling observed
under the lock. They never prune membership or resurrect a removed target.
Only `upsert_workset`, under planning-then-runtime lock order, may remove a
quiescent task; claimed, active, nonterminal, or pending targets cannot be
removed. Terminal attempt history remains durable even when a quiescent task's
current state row leaves membership, including legacy terminal attempts
without an `ended_at` value.

Durable landing is also product-layer orchestration. `blackdog` owns one
event-ledger transaction per attempt; `blackdog_core` supplies deterministic
append-once events and idempotent task finalization without learning Git or
WTAM policy. The normal transaction has exactly nine ordered phases:
`intent_recorded`, `source_prepared`, `canonical_commit_created`,
`target_updated`, `temporary_cleanup_complete`, `runtime_finalized`,
`land_event_recorded`, `task_cleanup_complete`, and `complete`. Its intent is
appended and made durable before any Git mutation, binds the complete landing
request and expected source/target state, and cannot be replaced by a retry
with different inputs. `runtime.json` remains schema v3; transaction progress
lives in additive `worktree.landing.phase` events.

The canonical Git message is a versioned product-layer envelope. Format 2 is
human-first: normalized completion-summary lines precede the machine section,
with the first line serving as the Git subject and each later line representing
one major change. Canonical Blackdog trailers remain the identity/proof plane.
Readers accept trailer-compatible legacy format 1 (no format trailer) during
the transition and format 2 (`Blackdog-Commit-Format: 2`), while rejecting an
explicit unknown or duplicated version.

Every retry re-proves the completed phase postconditions and resumes the same
attempt with the exact recorded request. Git preparation uses a deterministic
temporary worktree. Target movement is compare-and-swap against the recorded
base and never performs an implicit pull, reset, or overwrite. The task source
worktree and branch remain available until runtime finalization and the
append-once `worktree.land` record have succeeded; disposable source cleanup is
last. A product-layer attempt lock serializes landing with close, cleanup, and
reconciliation apply so those surfaces cannot finalize or remove the same
attempt concurrently.

The task's recorded `target_branch` is the landing and verification authority.
It may be a linked-worktree branch rather than the repository's primary branch;
agents never assume it is `main` and never switch it manually.

Before target update, close may branch the same ledger into durable abort. The
six product event types are `worktree.landing.abort`,
`worktree.landing.abort-cleanup`, `worktree.landing.abort-superseded`,
`worktree.landing.abort-runtime-finalized`,
`worktree.landing.abort-close-event-recorded`, and
`worktree.landing.abort-complete`. The terminal order is abort, temporary
cleanup, runtime finalization, close-event proof, then abort completion. The
only alternate order is abort, temporary cleanup, then supersession; it cannot
coexist with terminal abort stages and returns to normal landing.

The abort intent contains an immutable complete close request and exact
source/target/candidate evidence. Temporary abort cleanup never removes the
task source worktree or branch. They remain available for successor adoption
or late exact-candidate proof, even when close requested cleanup. Terminal
source removal still requires independent landed/patch-equivalent or exact
adoption-completion proof. `abort_complete` by itself is not disposability
proof, so unique unlanded work remains protected. An abort-in-progress
operation therefore resumes exact `task close`, not a prose-derived
approximation.

The core `task.finalization.request` is the abort decision's point of no return.
If the target contains the exact candidate after abort cleanup but before that
request, the product layer may append supersession proof and resume normal
landing. Once the request is durable, the core must converge its decision,
runtime replacement, and owned release/finish events even if target state later
changes. The product layer then records abort runtime finalization, appends and
proves the deterministic `worktree.close`, and finally records
`abort-complete`. A terminal runtime row before the final two product events is
still `abort_in_progress`, not a terminal transaction.

Ordinary close outside a landing abort has an equivalent product transaction
with a smaller ledger. The product layer appends a strict
`worktree.close.request` before core or Git mutation. That request freezes the
attempt, actor, terminal evidence, and source projection, and derives the one
core finalization, cleanup, and close-receipt identity. The core layer remains
the semantic owner for verifying `task.finalization.request`, its decision,
runtime replacement, claim releases, and `task.finish`; the product layer does
not duplicate or infer that proof.

Once core finalization is complete, product cleanup can remove only the exact
frozen source whose path, registration, branch, and HEAD still agree and whose
branch has independent safe-cleanup proof. Negative ownership is a successful
retention decision: dirty, detached, moved, foreign, primary, or unlanded
sources remain intact and are described by the final receipt. The product
layer then appends the deterministic schema-v1 `worktree.close`. The verified
request/core/cleanup/receipt chain is the close read model; failures after the
request return an exact guarded retry, while evidence conflicts return a
commandless blocker.

The close read model gates only competing mutations for the same task. Show
and recover project its exact retry, unrelated tasks and worksets continue,
and a complete transaction removes the gate. A complete predecessor remains
verifiable after a successor and its guarded retry is a no-op; an incomplete
predecessor can never coexist with a successor. Terminal failures classified
by pre-intent task/worktree land enter this same driver, so the land surface
does not maintain a second close protocol.

The transaction outcome read model is `landing_in_progress`,
`landed_complete`, `abort_in_progress`, or `abort_complete`. Those outcomes
select exact land resume, exact close resume, or post-terminal task routing;
pre-finalization supersession returns to `landing_in_progress`. This read model,
not chat context or error prose, owns recovery selection.

An `abort_complete` transaction may retain its source as a deterministic
successor workspace. Adoption is product-layer ownership transfer under the
attempt lock: the exact predecessor remains terminal, while a new attempt
reuses its worktree/ref and copies actor, prompt/request receipts, model,
reasoning, session, skill, setup, and handler lineage. Core reserves that
successor and its claim/start events atomically against the exact latest
predecessor; product `worktree.start` is deterministic and repairable. Live
handler probing is required before first product start evidence, while exact
durable evidence is authoritative on later reads. Every terminalizing surface
requires this start protocol to be complete before it can mutate Git, runtime,
or cleanup state.

The final target read immediately before successor reservation divides
ownership. If target already contains the predecessor candidate, no successor
is created and predecessor reconciliation remains authoritative. Otherwise
target drift returns a newly guarded adoption action without mutation. After
reservation, candidate arrival is successor-owned. A clean adopted workspace
that is behind or diverged rebases explicitly in that worktree; Blackdog never
performs the rebase implicitly.

Adopted-successor completion has two routes. Special candidate containment is
allowed only for the exact original source or a bounded no-merge rebase whose
stable patch id and changed paths equal the predecessor landing range, and
whose source is proven on the frozen completion target. Normal `task land`
allows successor work and retains the native landing transaction and sole
native `worktree.land`. Both routes first append one deterministic
`worktree.adoption.completion.intent` after a final target containment read.
Normal landing does so after temporary cleanup and before runtime finalization;
special completion does so before its own runtime finalization. After runtime
and the exact land event, one `worktree.adoption.complete` marker is durable
before task-source cleanup. The intent records native target-update proof and
the final completion read separately, so crash repair can use frozen evidence
even if the live target later moves. Cleanup has a narrow force-delete proof
for an exact validated completion; ordinary cleanup policy is unchanged.

Ordinary pre-intent target advancement has one additive, opt-in product-layer
correction path. A versioned `[landing]` policy authorizes high-level
`task land` to execute the existing worktree-local `git rebase --autostash`
operation once and then run the primary target profile's explicit validation
commands. An append-once `worktree.landing.correction` receipt binds the
request, policy, source/tree, target, rebase result, and bounded validation
proof before handing off to the unchanged landing transaction. No parallel
commit or target-update implementation exists. Conflict, validation failure,
or unproven Git state stops with a typed landing-agent blocker and preserves
the source. A second pre-intent target advance ends automation and returns the
existing `rebase_task_branch` command; post-intent target movement remains
inside the durable landing compare-and-swap and abort/recovery contract.

## Guardrails And Reporting

Task-class guard extension points live in the `blackdog` product layer. They
may consume `worktree preflight`, prompt preview, repo-skill context, handler
preview output, and attempt history, but they do not change the core
planning/runtime ownership boundary. The WTAM preflight surface stays focused
on workspace role, branch, dirty state, landing readiness, worktree inventory,
and the repo-local CLI path. The shipped startup guard classifies task prompts
before attempt start; deployment-class prompts must name the CI/GitHub Actions
route or explicitly approve local/emergency fallback before Blackdog creates a
new task envelope or starts an attempt. More class-specific checks for
credentials, external services, or approvals should stay layered around task
start and closeout rather than embedded into `blackdog_core`.

Environment/launcher repair expectations are owned by repo lifecycle and
handler orchestration. `repo install`, `repo update`, and `repo refresh`
validate configured handlers and repair repo-local runtime artifacts in the
repo-root scope. `worktree start` executes the handler plan for the task
workspace: worktree-local `.VE`, overlay/source-path wiring, root-bin fallback
links, and the worktree-local launcher. Handler actions and task-class guard
results are recorded as the attempt `setup_receipt` and on `worktree.start`
events when task execution starts. For skill-mode task starts, the product
layer also records bounded managed-skill provenance in that receipt: a
repo-relative path, file digest, source label, and provenance schema version.
This is additive durable evidence about the skill revision Blackdog read at
start, not model-consumption attestation, a planning field, or a new typed core
record.

Implementation-without-Blackdog detection lives in read models, not in runtime
mutation. `codex coverage` and `codex history` compare Codex session logs
against Blackdog attempts, classify implementation-like unlinked turns, attach
observed-vs-guidance environment issue evidence, and feed `repo table` and
`stats`. Those learning/report outputs support product tuning and audit without
copying full transcripts into Blackdog state. Repo cwd remains the ordinary
session-discovery boundary. Normal `task begin` captures invocation provenance
only from the current Codex thread and its exact session. It prefers an exact
unique prompt-hash match only when that match is the live turn (or there is no
live turn); otherwise one unique open turn is the invocation. It never chooses
the latest completed turn. Capture status, method, or bounded missing reason is
stored with the attempt, and this optional evidence is fail-open: no capture
failure may block or roll back work.

Reporting resolves attempt-owned session references before applying ordinary
repo-cwd pruning. An exact thread/session/turn may therefore add that one turn
even if its session cwd belongs to another repo. A legacy session reference
without a turn id may recover only one unique prompt-hash match inside that
exact referenced thread/session. Ambiguous, incomplete, missing, escaped, or
mismatched references add zero turns and remain bounded missingness. Sibling
turns and unrelated sessions are never imported by this overlay, and the
overlay does not mutate task/runtime/event state.

Codex hooks and environments belong in the `blackdog` product layer. Hook
handlers may inspect preflight state, active attempts, prompt hashes, and Codex
turn metadata to provide context or guardrails, but they must not bypass the
typed planning/runtime mutation APIs. `blackdog codex hook stamp` writes a
bounded append-only task-context stream under `codex/task-context.jsonl`; it is
observability evidence consumed by coverage/history, not the source of attempt
truth. Best-effort turn classification derived from transient hook input is
descriptive only: a `guarded` risk label cannot activate, satisfy, or bypass the
task-class guards around task execution. Codex environments should remain
convenience wrappers around Blackdog handlers and validation commands; they are
not the source of repo setup truth.

`blackdog codex link` is also a product-layer adapter. It resolves an active
attempt through the normal task read model and emits a supported Codex
new-local-chat deep link for the exact Blackdog-created task worktree. Blackdog
remains the owner of worktree creation, branch identity, landing, and cleanup;
the link does not create a Codex-managed worktree or mutate task state. Its
bounded prompt tells the new thread to recover through `task show` and the
machine-emitted `next_action`, rather than copying prompt artifacts or runtime
logic into the UI integration.

Product lifecycle observations also stay in `blackdog`, outside the typed core
runtime. Prompt, repo, stats, and task/worktree call sites may emit bounded
enum/hash-only rows to `observability/lifecycle-v1.jsonl` after an operation.
The writer uses a product-local nonblocking lock and absorbs every storage
failure, so telemetry cannot activate, block, recover, land, or change the
result of work. Lock acquisition never waits, but the bounded regular-file
write, flush, and fsync remain synchronous and have no universal latency
guarantee. A domain-neutral result adapter returns the exact input object and
derives enum/hash-only observations from mapping-style operation results.
Stable semantic IDs deduplicate retries. Stats reports stream health,
persistent capacity pressure, and explicit duplicate, missing, malformed,
unknown, and process-local write-failure counts; it does not reinterpret those
rows as task truth or claim complete operation coverage.

Supervised integration closeout is a coordination/reporting convention over
the same task-attempt model. Multiple workers can use the active Codex thread
for coordination, but durable state still flows through task begin/show/land/
close, attempt history, validation rows, residuals, follow-ups, and changed
paths. The architecture does not reintroduce a separate multi-agent runtime.
Task and attempt identity remain repo-local; a Codex thread/session/turn is
invocation provenance and may relate one source turn to attempts in more than
one target repo. It is not a global task owner or a second task envelope.

Landing reconciliation is an exceptional repair path for historical landings,
not an alternate landing workflow or the resume path for a nonterminal native
transaction. The product layer owns one canonical read-only Git proof shared
by explicit `task reconcile-landing` and candidate detection. It proves an
already-reachable, single-parent canonical Blackdog commit, exact trailers and
changed paths, bounded actor-mismatch compatibility evidence, and source patch
equivalence when the recorded source still resolves before the typed core may
change a latest historical failed/blocked runtime attempt to success.
Historical failure events remain append-only; apply adds
`task.landing.reconciled`. Deterministic correction identity makes a retry
repair an event missing after a completed runtime write.

Direct read-only `task show`, read-only `task recover`, and CLI `worktree show`
opt into bounded legacy candidate detection. Eligibility excludes active or
later attempts, claims, recorded landings, abandoned attempts, workspace
adoption, and every native landing transaction. The detector resolves the
exact start sentinel and scans at most 64 commits after it on target's
first-parent history, using one 65-row read so the boundary sentinel itself can
be observed. It reports `ready`, `none`, `unproven`, `ambiguous`,
`inconclusive`, or `error`; only `ready` emits one complete read-only dry-run
argv, never `--apply`. Internal recovery reads, tables, stats, registry data,
and Codex/session history do not invoke this scan. Detection does not write the
runtime/event ledger or mutate Git, refs, the index, or either worktree.

There is one native exception: an `abort_complete` transaction whose exact
recorded canonical candidate later becomes reachable from the target. That path
re-proves the complete abort chain and retained-source or exact independently
authorized cleanup evidence, appends exact transactional `worktree.land`
evidence, and honors the
original cleanup choice. It is available to native blocked/failed aborts and is
the only reconciliation eligibility for an abandoned attempt; arbitrary
abandoned historical rows remain outside the repair contract. Every
nonterminal native transaction instead returns exact land or close resume.
The product must pass typed eligibility containing the exact attempt,
transaction, and canonical candidate into core finalization. Core rejects an
abandoned correction without that explicit eligibility or when any identity
differs, including on idempotent repair after a prior runtime write.

## Current Shipped Surface

The current coherent product surface on top of the new core is:

- `blackdog init`
- `blackdog summary`
- `blackdog snapshot`
- `blackdog stats`
- `blackdog local-repo add`
- `blackdog local-repo list`
- `blackdog local-repo remove`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog codex link`
- `blackdog codex coverage`
- `blackdog codex history`
- `blackdog codex hook stamp`
- `blackdog repo install`
- `blackdog repo bind`
- `blackdog repo table`
- `blackdog repo archive`
- `blackdog repo unarchive`
- `blackdog repo unbind`
- `blackdog repo analyze`
- `blackdog repo scaffold`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog task begin`
- `blackdog task show`
- `blackdog task recover`
- `blackdog task cancel`
- `blackdog task reopen`
- `blackdog task land`
- `blackdog task reconcile-landing`
- `blackdog task close`
- `blackdog task cleanup`
- `blackdog worktree preflight`
- `blackdog worktree table`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog worktree cleanup`

The `task` family is the default same-thread WTAM path and is what generated
repo-local skills use for `$<repo-name> do ...` requests. The skill may compose
an execution prompt and pass it with `prompt-mode=skill` while recording the raw
user request as separate prompt lineage. `task begin` uses the operator's
current workspace as the routing context when that workspace belongs to the
managed Git repository, so normal linked worktrees become target branches and
receive changes only through nested task worktrees.
The `worktree` family remains the explicit low-level WTAM path when an operator
needs preflight, preview, or recovery control. Direct workset authoring remains
available only behind an explicit opt-in for planned-task migration and repair;
it is not part of the generated repo skill surface.

The repo lifecycle family ships in `blackdog` as
analyze/bind/table/scaffold/install/update/refresh/archive/unarchive/unbind,
prompt preview/tune, and attempt inspection.

For repos other than Blackdog itself, `repo analyze` is the read-only
conversion entrypoint. It inventories agent docs, skills, `.VE`, launcher and
profile state, then emits findings plus a proposed conversion plan before any
repo files are mutated. `repo bind` is the first-class membership spelling for
the install contract and wraps the same implementation as `repo install` while
reporting action `bind`. `repo install` and `repo update` default to a managed
Blackdog source checkout under the control root, sourced from GitHub.
`--source-root` is the explicit local override.
`repo scaffold` is the new-project entrypoint: it creates or reuses a target
git repo, optionally seeds starter docs from an exemplar repo, and then
delegates to `repo install` so generated project repos get the same minimal
repo-local skill and managed contract as converted repos.
When install has to create a fresh profile, it seeds routed docs from
`AGENTS.md` plus common host-repo docs that already exist, and it writes a
managed Blackdog contract block into `AGENTS.md` so WTAM rules live in repo
docs instead of only in the generated skill. `repo refresh` rewrites that
managed `AGENTS.md` block, regenerates the repo-local skill and Codex
`agents/openai.yaml` metadata, and is also the shipped cleanup path for
removing known backlog-era artifacts, obsolete Blackdog-managed skill
directories, stale generated skill auxiliary files, and the one stale
removed-orchestration run directory.
Repo lifecycle commands report when they changed repo-visible managed files.
Those changes intentionally leave the primary checkout dirty until an operator
commits, lands, reverts, or explicitly reports them, so generated skills and
managed contracts require a closing `git status --short` check.

`repo table` is a cross-repo operator-read surface. Like `stats`, it accepts
exact repeated project roots, read-only discovery roots scanned for
`blackdog.toml`, or the explicitly selected user-local registry. These three
scope modes are mutually exclusive and deduplicate by resolved project root.
It reads each repo's runtime, attempt, and optional Codex coverage views. Its
default columns are task/attempt oriented: live state is reported
under `current_*`, historical attempt diagnostics are reported under
`window_*`, structured failure classes are counted in the window diagnostics,
Codex turn coverage includes token totals plus the longest completed single
turn for the repo, and legacy workset storage counts are hidden unless an
operator explicitly asks for the migration/debug column. Registry scope is
never implicit for `repo table`; callers select it with `--registry`.
`local-repo` manages that explicit operator-curated convenience set. Optional
`[project].status` in `blackdog.toml` controls membership visibility:
missing means active, `archived` hides the repo from table output unless the
operator asks for archived rows. `repo archive` and `repo unarchive` update
only that status key. `repo unbind` is the inverse lifecycle cleanup surface:
it previews by default, strips only the managed `AGENTS.md` block on confirm,
removes Blackdog-managed profile/skill/launcher/control paths, preserves
repo-owned text and unrelated dirty files, and preserves external control dirs
outside the repo or git common dir.

`stats` is the first-class cross-repo metric read model for task, attempt, and
Codex-session counts. It accepts explicit project roots, explicitly supplied
discovery roots scanned for `blackdog.toml`, or an explicitly selected
user-local registry. Bare `stats` alone retains a compatibility registry
fallback; other fleet surfaces require explicit scope. Discovery is read-only
and does not populate the registry. Stats
buckets attempt and Codex turn counts by `started_at` in a selected timezone,
deduplicates exact aliases for the same resolved Blackdog project root, reports
cleanup health for terminal branch/worktree attempts, exposes linked/unlinked
Codex coverage counts, and keeps nested repos separate when they have distinct
profiles. Per-repo rows may each show a relationship to one shared source turn;
fleet session, turn, tool, and token totals count its `(thread_id, turn_id)`
identity once.

Repo-local env/runtime setup is now owned by explicit handler blocks in
`blackdog.toml`, not by skill text or ad hoc bootstrap code. The shipped v1
handlers are:

- `python-overlay-venv` for the repo-root `.VE`, worktree-local overlay `.VE`,
  simple editable source path replay, and root-bin fallback linking
- `blackdog-runtime` for the repo-local or worktree-local `blackdog` launcher
  plus managed-source resolution

These commands exercise one end-to-end vertical slice:

1. create or update planning and runtime state
2. start one same-thread task envelope in one command while optionally tuning
   the recorded execution prompt
3. inspect the WTAM contract before kept changes when the operator needs an
   explicit planned-task flow
4. preview one branch-backed task execution plan, including prompt receipt
   metadata, repo contract inputs, and the ordered handler plan for the task
   worktree
5. start one branch-backed task worktree with a prompt receipt, a provisioned
   worktree-local `.VE`, repo-root overlay wiring, worktree-local editable
   source paths, root-bin fallback links, a worktree-local launcher, and real
   git execution identity while claiming both the workset and the task
6. inspect one active or latest task attempt for recovery-oriented worktree and
   claim state, including missing branch/worktree references
7. land one successful task attempt through a canonical landed commit while
   recording structured result, validation, commit lineage, releasing claims,
   and cleaning up the task worktree by default
8. close one blocked, failed, or abandoned task attempt without landing code;
   abandoned attempts cancel the task by default
9. cancel or reopen planned work so stale tasks do not pollute normal reads
10. clean up any retained or leftover task worktree
11. read task-first summary/status, hiding canceled work unless explicitly requested
12. identify the next runnable tasks
13. emit a machine-readable task/attempt-first runtime snapshot, with legacy
    nested workset rows only on explicit request

## Deferred Or Removed Product Code

This repo no longer keeps legacy backlog, board, inbox, bootstrap, or
compatibility-plan code as dormant historical baggage. Legacy backlog-era
multi-agent orchestration code remains removed from the mainline repo surface.
The only remaining migration seam is cleanup of leftover removed-orchestration
control-root artifacts during `repo refresh`.
