# Architecture

Blackdog is a local task and attempt runtime. It supplies a deterministic CLI
protocol around AI-driven repository work without becoming an agent runtime,
transcript store, background coordinator, or repository-policy engine.

The core invariant is direct:

> One executable task has one durable identity, zero or more append-ordered
> attempts, and no more than one active attempt.

## Layers

### `blackdog_core`

The core package owns only durable, provider-neutral semantics:

- Repository profile and control-path resolution
- The canonical task store
- Task and attempt records
- Prompt and provider-reference records
- Task state transitions and exclusive ownership
- Atomic replacement and interprocess locking
- Append-once event identity
- Task and attempt read models

It does not know how to create a Git worktree, invoke a repository handler,
compose a provider URL, run a landing transaction, or generate a managed skill.

### `blackdog`

The product package owns repository operations over the core:

- Git worktree creation and ownership proof
- Task begin, recovery, landing, close, reconciliation, and cleanup
- Repository guards and validation execution
- Environment handlers
- Prompt preview and replay artifacts
- Provider session references and coverage reporting
- Repository installation, refresh, membership, and fleet reporting
- Best-effort bounded observability

### `blackdog_cli`

The CLI package parses arguments, calls product or core operations, and renders
typed results. It contains no lifecycle decision logic.

## Durable State

The private control root contains three kinds of durable evidence:

1. `runtime.json`, the only mutable task-state authority
2. `events.jsonl`, the append-only semantic mutation ledger
3. Content-addressed request and execution prompt artifacts

`blackdog.toml` is repository-visible configuration, not task state. Optional
observability and derived history files are reporting evidence, never lifecycle
authority.

### Task store

`runtime.json` schema 4 stores top-level `tasks`. Each task row contains:

- Identity, title, and creation time
- Current status, update time, actor, and note
- Structured failure and recovery fields
- Append-ordered attempts

A task's durable intent is its title plus the request and execution prompt
receipts on its attempts. Prompt bodies live in content-addressed artifacts.
Planning detail from older formats remains only in immutable migration
archives.

There is no separate planning file or claim table. An in-progress attempt is
the exclusive execution claim. Start atomically appends that attempt and moves
the task to `in_progress`; finish atomically writes its terminal evidence and
updates the task. This representation cannot create an orphan claim without an
attempt.

Task IDs are unique within a repository. Attempt IDs are unique within the
store. Fleet-level identity is the repository identity plus the task or attempt
identity.

Higher-level task relationships are deferred. When introduced, they must be
direct references between executable tasks and must not change the meaning of
task ownership.

### Attempts

An attempt records one actor's execution of one task. Its durable fields cover:

- Status and timing
- Actor, execution model, model, and reasoning effort
- Worktree path and role, source branch, target branch, and start commit
- Request and execution prompt receipts
- Optional provider thread and turn reference
- Setup receipt and handler evidence
- Changed paths, validations, residuals, and follow-up candidates
- Source commit and canonical landed commit
- Failure class, recovery action, and issue flags

The last appended attempt is the latest attempt even when timestamps tie. An
attempt is never reordered during an update.

### Events

Events provide audit and crash-repair evidence for semantic mutations. An event
has a stable ID, type, timestamp, actor, and payload. Operations that may retry
derive event IDs from versioned namespaces and canonical task, attempt, request,
or transaction identity.

Appending the same event ID with identical semantic content is a no-op. Reusing
an ID with different content is corruption and fails closed. New task-only
formats use new namespace versions; identities from an older format are never
silently adopted.

### Prompt artifacts

Normalized request and execution prompts are persisted privately by SHA-256.
Attempt receipts retain the digest, mode, source reference, recorded time, and
artifact-relative path. Runtime rows need not duplicate the full prompt.

The request and execution roles remain distinct even when their text matches.
Recovery reopens an existing task only after both roles match durable lineage.

## Concurrency and Atomicity

The file-backed store uses one adjacent interprocess lock across load, validate,
mutate, and atomic replace. Writers always merge against the state observed
under that lock and preserve unrelated tasks.

Long Git, handler, and validation operations do not run while holding the core
state lock. Product transactions record deterministic intent and phase evidence
around those effects, then re-enter the short core mutation boundary.

Per-attempt product locks prevent competing landing, close, cleanup, and
reconciliation operations for the same attempt. Unrelated tasks may continue.

## Structured Lifecycle Results

Every task lifecycle command returns an operation result with:

- Operation and observed status
- Task and attempt status
- Mutation-started, mutation-completed, and mutation-phase facts
- Bounded failure code
- Exactly one typed `next_action`

`next_action.kind` is one of:

- `command`: execute its exact nonempty `argv`
- `choice`: select one complete emitted choice
- `complete`: stop successfully
- `blocked`: stop and satisfy its typed required inputs

Display text, reasons, errors, and summaries are explanatory only. They are
never executable authority. An emitted command carries complete task and retry
identity so an agent does not reconstruct hidden guards.

## Task Lifecycle

### Begin

`task begin` is the normal implementation entrypoint. It:

1. Validates the repository, managed skill when requested, guard results, Git
   base, handler readiness, and prompt inputs.
2. Persists content-addressed request and execution prompt artifacts.
3. Creates a repository-unique task or verifies an exact existing-task resume.
4. Creates and registers a task branch and worktree.
5. Executes repository-configured handlers in that worktree.
6. Atomically starts the attempt and records deterministic start evidence.

If a failure happens after a retained boundary, the result reports a partial
mutation and emits the exact recovery action. It does not pretend the operation
failed before mutation.

### Show and recover

`task show` derives current lifecycle state without mutating it. `task recover`
adds bounded recovery inspection and may execute only explicitly requested,
typed repair operations.

Recovery verifies durable task state, attempt lineage, event evidence, Git
references, worktree registration, prompt artifacts, and active product
transactions. Missing and operational-error states remain distinct.

### Cancel and reopen

Cancel moves an inactive task to `canceled`; reopen returns a canceled task to a
restartable state. Both operations are identity-bound state transitions. They
cannot take over an active attempt, and exact retries are no-ops.

### Land

Landing is a durable phased transaction:

1. Record immutable intent and supplied completion evidence.
2. Freeze and, when required, commit the task source tree.
3. Create the canonical landed commit.
4. Compare-and-swap the recorded target branch.
5. Remove transaction-owned temporary Git state.
6. Finalize attempt and task runtime evidence.
7. Append the canonical land event.
8. Remove the task worktree and branch unless retention was requested.
9. Record completion.

The target branch stored on the attempt is authoritative. A retry verifies each
completed phase before advancing. It never creates a second canonical commit or
weakens proof after a crash.

The first land request requires a nonblank human summary and at least one real
validation row. Automatic stale recovery is optional repository policy. When
enabled, it may perform one guarded rebase and the configured validations; it
returns a typed blocker on conflict, unsafe state, failed validation, or
exhausted retry.

### Reconcile landing

`task reconcile-landing` is a current-format recovery surface for the narrow
case where canonical Git evidence exists but terminal runtime evidence is
incomplete. It requires exact task, attempt, candidate commit, target, actor,
trailers, changed paths, and source equivalence proof.

The read operation never applies a correction implicitly. Only an exact emitted
action may include `--apply`. Unsupported older commit or store formats are not
searched or interpreted.

### Close

Close terminates an attempt without landing code. Its immutable request records
terminal status, summary, validations, residuals, follow-ups, failure fields,
and cleanup intent. Core finalization precedes optional source cleanup, and the
terminal close event records what was actually removed or retained.

Blocked, dirty, moved, foreign, or unproven source workspaces are retained. A
request for cleanup is not itself proof that deletion is safe.

### Cleanup

Cleanup removes only a task worktree and branch whose ownership and disposal
proof match the exact task attempt. Missing resources are idempotent success;
ambiguous ownership or an unlanded source blocks mutation. If worktree removal
succeeds but branch deletion or event append does not, the result reports the
partial phase and emits a safe retry.

## Worktree Isolation

Kept implementation changes belong in branch-backed task worktrees. Blackdog
proves the primary checkout is safe, selects and records the actual target
branch, uses deterministic task branch/path derivation, and treats registered
Git state as part of ownership proof.

`worktree preflight` and `worktree table` are read-only operator surfaces. All
low-level worktree mutation aliases have been removed; normal mutation flows
through task lifecycle commands.

Repository handlers remain configured in `blackdog.toml`. The default runtime
handler uses an immutable, digest-addressed standalone archive under the control
root. Optional Python project handlers may create worktree-local `.VE` tool
environments; task execution and recovery do not depend on those environments.
Blackdog self-development explicitly executes the task checkout's source while
recovery retains its immutable archive. See [runtime distribution](RUNTIME_DISTRIBUTION.md).

## Provider References

Blackdog stores bounded references to provider-owned conversations, including
thread, turn, timing, prompt-hash, and capture status when available. A local
session path is evidence, not durable global identity.

Coverage and history compare those references with attempts without importing
full conversations. Missing or ambiguous resolution remains explicit. Task
results, prompt receipts, validations, commits, and closeout evidence must stand
without the external conversation.

## Repository Policy

Blackdog owns generic protocol and evidence. Repositories own policy through:

- Ordered validation commands
- Optional guards
- Environment handlers
- Routed documents
- Optional guarded automatic stale recovery

Managed skills route agents into the CLI contract. They must not contain a
second lifecycle implementation.

## Derived Reporting

`summary`, `snapshot`, attempt history, repository tables, Codex coverage, and
`stats` are derived read models. They do not create tasks or rewrite runtime
state.

Optional lifecycle observability is bounded, append-only, fail-open, and free
of prompt or path content. It can report command health but cannot prove
lifecycle completion; canonical task and event evidence remains authoritative.

## Non-goals and Deferred Stages

The current architecture deliberately excludes:

- Automatic task decomposition, hierarchy, dependencies, or next-task choice
- A background coordination service or workflow language
- Online prompt optimization or prompt self-modification
- Transcript storage
- A built-in multi-provider execution runtime
- A dashboard
- Readers for superseded state formats

Blackdog ships reproducible Python release archives and defaults to a runtime
independent of repository `.VE` environments. The archive requires system Python
3.11 or newer; CI builds and exercises it on Linux and macOS. A bundled
interpreter or a language port requires separate evidence and is not shipped.

The accepted [execution and outcome plan](EXECUTION_OUTCOMES_PLAN.md) defines
this stage, typed outcome evidence, and the proof required for completion.
