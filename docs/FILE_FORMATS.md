# File Formats

Blackdog's durable contract is machine-owned and JSON-first. Agents use the CLI
and never edit these files directly.

By default, `paths.control_dir = "@git-common/blackdog"` resolves beneath the
Git common directory so every linked worktree sees the same task state.

## Canonical Files

The private control root contains:

- `runtime.json` — canonical mutable task state
- `events.jsonl` — append-only semantic mutation evidence
- `prompts/sha256/<digest>.txt` — content-addressed prompt replay artifacts

The repository root contains:

- `blackdog.toml` — repository profile, handlers, guards, validation, landing
  policy, and routed-document configuration

There is no second task-authoring or membership store. Optional reporting files
are derived evidence and cannot authorize lifecycle mutation.

## `runtime.json`

Current version markers:

```json
{
  "schema_version": 4,
  "store_version": "blackdog.runtime/v4",
  "tasks": []
}
```

The loader requires exactly the supported version. An older or unknown version
fails closed and is never rewritten by the active runtime.

File-backed mutation holds an adjacent interprocess lock across load,
validation, mutation, and atomic replacement. On POSIX the lock file is
`runtime.json.lock`; the standard-library fallback may use
`runtime.json.lockdir`. Lock artifacts are coordination only.

### Task rows

Each `tasks` row contains:

- `id`
- `title`
- `created_at`
- `status`
- optional `updated_at`
- optional `actor`
- optional `note`
- optional `failure_class`
- optional `recovery_action`
- `prompt_issue`
- `operator_issue`
- `attempts`

A task's durable intent is its title plus the request and execution prompt
receipts on its attempts. Prompt bodies live in content-addressed artifacts.
Planning detail from older formats remains only in immutable migration
archives.

Task IDs are nonempty and unique within the file. Attempts are stored directly
on their task row; the same attempt ID may not occur anywhere else in the
store. The order of `attempts` is authoritative. Updating an attempt preserves
its position and a successor appends after its predecessor.

Allowed task statuses:

- `planned`
- `in_progress`
- `blocked`
- `done`
- `canceled`

There is no separate claim row. Exactly one in-progress attempt, when present,
is the task's exclusive execution ownership. A task in `in_progress` must have
that canonical active attempt, and an inactive task may not have one.

Failure classes are bounded:

- `dirty_primary`
- `stale_branch`
- `missing_worktree`
- `no_changes`
- `superseded`
- `abandoned`
- `unknown`

`recovery_action` supplies bounded machine-readable classification alongside
free-text summary or note. `prompt_issue` and `operator_issue` are booleans.

### Attempt rows

Each attempt contains:

- `attempt_id`
- `task_id`
- `status`
- `actor`
- `started_at`
- optional `ended_at` and `elapsed_seconds`
- optional `summary` and `note`
- optional `workspace_identity` and `workspace_mode`
- optional `worktree_role`, `worktree_path`, and `branch`
- optional `target_branch`, `integration_branch`, and `start_commit`
- optional `execution_model`, `model`, and `reasoning_effort`
- optional `codex_session`
- optional `prompt_receipt` and `user_prompt_receipt`
- `changed_paths`
- `validations`
- `residuals`
- `followup_candidates`
- optional `commit` and `landed_commit`
- optional `failure_class` and `recovery_action`
- `prompt_issue` and `operator_issue`
- optional `setup_receipt`

Allowed attempt statuses:

- `in_progress`
- `success`
- `blocked`
- `failed`
- `abandoned`

Only `in_progress` is active, and an active attempt has no `ended_at`. Terminal
attempts remain durable history.

`commit` is the task-branch source commit. `landed_commit` is the canonical
commit created on the recorded target branch. These identities are deliberately
separate.

### Validation rows

Each validation contains:

```json
{"name": "unit-tests", "status": "passed"}
```

Status is `passed`, `failed`, or `skipped`. Ordinary landing treats these as
caller-supplied evidence and never invents them. Product-owned automatic stale
recovery may add only its specifically reserved validation row after actually
running configured commands successfully.

### Prompt receipts

A prompt receipt contains:

- optional `text` only where explicitly allowed for older in-memory operations
- `prompt_hash`, a lowercase SHA-256 digest
- `recorded_at`
- optional `source`
- optional `mode`
- optional `replay_artifact_path`

New task starts persist normalized prompt text in the content-addressed artifact
and retain the reference in runtime. `replay_artifact_path` is relative to the
control root, must name the matching digest, and cannot escape that root.

`prompt_receipt` is the execution input. `user_prompt_receipt` is the triggering
request lineage. When both roles are identical, read models may show a shared
prompt view; durable retry validation still knows which role was supplied.

Prompt modes are bounded by the current CLI contract. Unsupported modes fail
before task mutation.

### Provider session reference

`codex_session`, when present, contains:

- `thread_id`
- optional `session_path`
- optional `turn_id` and `turn_started_at`
- optional `user_prompt_hash` and `execution_prompt_hash`
- optional bounded capture status, method, and missing reason

This object references provider-owned dialogue. It does not contain the
conversation. `session_path` is local evidence, not portable identity, and its
absence does not invalidate the task attempt.

### Setup receipt

`setup_receipt` is bounded product evidence recorded at start. It may contain:

- Schema version and checked time
- Status and blockers
- Repository guard receipts
- Handler probes and effective runtime/source modes
- Worktree-local launcher paths
- Managed-skill provenance
- Atomic start identity
- Base-ref, base-commit, and primary-worktree evidence

Guard receipts bind guard ID, phase, configuration hash, status, reason, and
required inputs. Skill provenance binds a repository-relative path, SHA-256,
and bounded source. These receipts prove what Blackdog checked and associated
with the attempt; they do not attest that a model followed the instructions.

## `events.jsonl`

Each line is one JSON object:

```json
{
  "event_id": "sha256-identity",
  "type": "task.start",
  "at": "2026-08-30T12:00:00+00:00",
  "actor": "codex",
  "payload": {}
}
```

Current core event families cover:

- `task.create`
- `task.start`
- Task status-transition request, decision, and owned result
- Task finalization request, decision, release, and finish
- Current-format landing reconciliation

Product event families cover:

- Worktree start
- Landing correction and phased landing
- Landing abort and close evidence
- Land, close, and cleanup completion
- Retained-workspace recovery evidence where supported

Exact event names and payload keys are versioned contracts in the implementation
and tests. Every task-scoped event identifies `task_id`; attempt-scoped events
also identify `attempt_id`.

Deterministically identified events use strict canonical JSON semantics. Object
key order and tuple/list input spelling do not change identity; booleans and
numbers remain type-distinct. Non-string keys, non-finite numbers, or values
outside JSON are rejected.

Repeating an exact ID and semantic payload is a no-op. Reusing an ID for a
different type, actor, or payload is a hard conflict. Event IDs and transaction
hashes use task-only versioned namespaces and are not shared with superseded
formats.

### Start evidence

Task creation and attempt start are separate semantic facts. Runtime mutation
atomically creates the task or appends its active attempt. Deterministic event
append follows and can be repaired from canonical state. A crash between these
boundaries therefore cannot create duplicate execution ownership.

An exact resume binds:

- Task and predecessor attempt
- Receiving actor
- Execution and request prompt hashes and modes
- Task state generation
- Source branch/worktree evidence when retained

A conflict fails before a successor attempt or new Git workspace is adopted.

### Finalization evidence

A terminal operation first records an immutable finalization request. Its
decision binds the observed pre-task row, stable ending time, expected terminal
task and attempt rows, and the owned trailing event identities. Runtime
replacement occurs under the same state lock; release and finish events append
afterward and remain repairable.

Exact completed retries change neither runtime nor event bytes. A pending
generation blocks competing mutation for its task but does not gate unrelated
tasks.

### Landing evidence

Landing uses append-once product phase events keyed by a deterministic
transaction ID. Normal phases cover intent, source preparation, canonical
commit creation, target update, temporary cleanup, runtime finalization, land
event, task-source cleanup, and completion.

The immutable intent binds the exact task/attempt/actor, source and target,
summary, validations, changed paths, cleanup request, and prompt/model
provenance used for commit trailers. Each retry rederives and verifies completed
phases before advancing.

An abort branch records immutable close evidence and retains task source unless
independent cleanup proof exists. A terminal abort is not landing proof.

### Close evidence

A close request binds one task and active attempt, actor, terminal status,
summary, validation and follow-up evidence, failure fields, and cleanup intent.
It precedes core finalization and source cleanup.

The terminal close event records actual cleanup disposition. Requested cleanup
may be refused while close still completes; the evidence distinguishes retained
source from completed deletion.

### Cleanup evidence

Cleanup events identify the exact task, optional terminal attempt, branch, and
worktree path and prove both workspace and branch absence. Their deterministic
identity makes post-deletion event repair safe. Ambiguous paths or branches fail
before removal.

## Task Lifecycle Command Results

Lifecycle command results are typed outputs, not another durable state file.
JSON retains the command-specific top-level wrapper and one uniform operation
result.

Uniform fields:

- `operation`
- `operation_status`
- `task_status`
- `attempt_status`
- `disposition`
- `mutation_started`
- `mutation_completed`
- `mutation_phase`
- `failure_code`
- `next_action`

`next_action` fields:

- `action_id`
- `kind = command | choice | complete | blocked`
- `disposition`
- `reason_code` and `reason_detail`
- `display`
- `argv` and `command`
- `choices` and `alternatives`
- `required_inputs`
- `safety_class` and `mutation_class`

A command has exactly one nonempty argv. A choice has no primary argv and only
complete child actions. Complete and blocked actions contain no executable
argv. `command` is a rendering; `argv` is authoritative.

Mutation fields describe observed effects. A post-runtime cleanup refusal is
partial, not success. A proven no-op reports no mutation. An operation that
changed Git but still owes an event reports that exact pending phase.

## Canonical Git Commit Format

The first nonblank normalized completion-summary line is the Git subject. Each
later nonblank line is one human-readable body item. Machine trailers follow:

- `Blackdog-Task`
- `Blackdog-Attempt`
- `Blackdog-Actor`
- `Blackdog-Status`
- `Blackdog-Commit-Format`
- optional target branch, execution model, model, and reasoning effort
- execution prompt hash, source, and mode
- distinct request prompt hash, source, and mode when applicable
- one changed-path trailer per path
- validation, residual, and follow-up trailers supplied at land time

Current-format reconciliation requires exact supported trailers. Missing,
duplicated, or unsupported format evidence is not guessed.

## `blackdog.toml`

Required top-level sections:

- `[project]`
- `[paths]`
- `[taxonomy]`
- one or more `[[handlers]]`

Optional sections:

- `[landing]`
- `[[guards]]`

`[paths]` identifies the control root, runtime file, event file, and worktree
root. It does not identify a second task-state file.

`[taxonomy].doc_routing_defaults` is an ordered catalog. Generated instructions
tell agents to select relevant entries; listing a document does not inject its
text into every prompt.

Each handler has an ID, kind, enabled state, and kind-specific setup fields.
The default Blackdog handler selects `source_mode = "installed-runtime"` and
needs no Python project handler. It stores release archives under the configured
control root. Explicit Python and legacy source handlers retain their existing
configuration. Effective setup actions and probes are recorded on attempts.
Release archives carry a versioned source manifest; their SHA-256 filenames bind
exact recovery executables. See [runtime distribution](RUNTIME_DISTRIBUTION.md).

Each guard has a unique ID, phase, command, timeout, required flag, and optional
message. Guard commands receive bounded task inputs and return typed pass or
block results. Blackdog owns protocol; the repository owns the policy decision.

Landing policy may enable one automatic stale rebase and sets validation
timeout. Without complete policy, landing returns a manual typed action.

## Managed Repository Files

Repository lifecycle commands may create or maintain:

- `AGENTS.md` managed contract block
- `.codex/skills/<repo-slug>/SKILL.md`
- Managed skill metadata
- Immutable control-root release archives and their selected runtime reference
- Optional project environment artifacts configured by explicit handlers

Generated skills route the agent to task commands and contain no lifecycle
implementation.

After bind, install, update, or refresh, callers inspect `git status --short`
and account for every repository-visible change.

## Optional Observability

`observability/lifecycle-v1.jsonl` is bounded, append-only, best-effort product
evidence. It is not task state and any write failure leaves the command result
unchanged.

Rows contain schema version, project digest, command surface, bounded operation
key digest, outcome, reason, enumerated labels, observation ID, and time. Prompt,
summary, note, error, command, branch, changed-path, and raw path text are never
stored.

The file has a size cap, duplicate detection, nonblocking product-local lock,
bounded read failure classification, and no silent rotation. Deleting it removes
optional observations only.

## User-local Files

Blackdog uses `BLACKDOG_HOME` when set, otherwise `$CODEX_HOME/blackdog` or
`~/.codex/blackdog`.

### `local-repos.json`

Explicit repository registry with schema version, update time, and rows
containing project name/root, status, and timestamps. It is reporting
convenience, not task truth.

### `codex/session-cache-v1.json`

Parsed provider session cache keyed by source path identity, size, and modified
time. Entries contain bounded session/turn metadata, environment issue classes,
tool counts, timing, and token counters. They do not contain full transcripts.

## Derived `history.jsonl`

`codex history --write` may write a compact private history export. Rows contain
bounded provider turns, task/attempt relationships, prompt hashes, timing,
validation, failure, and environment classifications. Canonical truth remains
the task store, event ledger, Git history, and provider session source.

## State Cutover

Task-only schema 4 is an explicit format boundary:

- Active code reads and writes schema 4 only.
- Unknown or older stores fail closed without mutation.
- Historical control directories are preserved as immutable archives when
  needed for audit.
- `blackdog repo migrate` is the explicit lifecycle boundary for supported older
  stores; ordinary runtime readers never interpret legacy schemas. The command
  archives original bytes and identity mappings before publishing schema 4.
- Old event identities are never imported into the new ledger with changed
  semantics.

This boundary preserves evidence while keeping the active runtime limited to
current-format state.

### Supported store migration

`repo migrate` currently supports runtime schema 3 with its matching planning
schema 1 when every task is terminal and all claims and retained worktrees are
absent. It preserves completed task/attempt evidence with deterministic IDs,
flattens legacy session-capture metadata, and validates existing content-addressed
prompt artifacts using the normal schema-4 validator. It never invents completion
or takes over an active claim. Other schemas and unfinished work fail closed.

The default preview performs no target writes and returns counts, a source digest,
archive location, and an exact guarded apply action. Under runtime/event locks,
apply rechecks the digest, writes private original files, hashes and ID mappings
under `migrations/<digest>/`, and records `migration-pending.json` before replacing
active files. All normal runtime loads reject a pending migration. Replay validates
archived bytes, publication hashes, mappings, and allowed intermediate file states
before completing. Unexpected changes stop recovery without overwriting them.
Original event IDs remain only in the archived ledger; the current ledger begins
with one migration event. The old planning file is removed only after its archived
copy and the new runtime are durable. Prompts and historical sidecars remain
untouched. A completed retry is a no-op.

## Typed outcome evidence (schema 1)

The existing event ledger additionally accepts strictly validated
`task.evidence.definition`, `task.evidence.assessment`,
`task.evidence.intervention`, `task.evidence.validation-intent`,
`task.evidence.validation-result`, and `task.evidence.phase` families.
Each payload contains exactly `schema_version`, `task_id`, `attempt_id`, and
`data`. Identities bind event kind, task, attempt and immutable request ID;
measured clocks and durations are not identity inputs.

Definitions are immutable, criterion assessments form predecessor chains, and
validation results reference durable invocation intents. Machine command-result
contracts live in `blackdog_core.validation`. Binding and outcome contracts
live in `blackdog_core.evidence`; known malformed or unsupported records fail
closed in outcome reporting. Setup receipts may include a schema-1
`setup_measurement` with bounded handler duration in milliseconds, monotonic
source and explicit missingness.

This is an additive event format. Runtime v4 and historical bytes are preserved;
there is no evidence backfill, task-store migration or new landing authority.
[Outcome evidence](OUTCOME_EVIDENCE.md) documents field schemas and semantics.
