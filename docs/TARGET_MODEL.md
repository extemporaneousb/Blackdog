# Target Model

This document describes the vNext model Blackdog is now implementing.

For the supported product workflows and v1 target, use
[docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md). This document defines the object
model, not the product story set.

The breaking premise is simple:

- humans author docs, design intent, approvals, and prompts
- agents mutate planning and runtime state through typed Blackdog operations
- markdown is not the canonical planning layer

## Locked Decisions

These decisions are no longer provisional:

- `Workset` is the top-level durable planning owner
- tasks live inside a workset-owned DAG
- `epic`, `lane`, and `wave` were removed as durable planning concepts
- `planning.json` is the canonical planning store
- `runtime.json` is the canonical mutable runtime store
- `events.jsonl` remains append-only audit history
- claims attach to both worksets and tasks
- the first-class execution models are `direct_wtam` and `workset_manager`
- each successful `direct_wtam` task attempt lands as one canonical Blackdog
  commit
- non-worktree execution is not part of the product model
- `blackdog_cli` stays thin and only dispatches into core/product code

## Audience Model

The old backlog taught humans to shape semantic truth in markdown.
That was the wrong audience model.

The vNext audience split is:

- humans write repo docs, design docs, explicit approvals, and prompt material
- agents write and curate worksets, tasks, and runtime state through the CLI or
  typed library operations

Humans may inspect the machine files directly, but those files are not intended
to be hand-edited as the primary workflow.

## Core Objects

### `Workset`

The primary planning and execution container.

A workset owns:

- scope
- task DAG
- visibility boundary
- policies
- canonical exported workspace identity
- branch intent

### `WorksetClaimRecord`

The active claim over one workset.

It carries:

- actor
- execution model
- claimed timestamp
- optional note

### `TaskSpec`

The durable specification of one executable unit inside a workset.

It carries:

- stable task id
- title
- intent
- optional description
- dependency ids
- relevant paths/docs/checks
- extension metadata

### `TaskRuntimeRecord`

The mutable execution state for one task inside a workset.

The current vNext runtime intentionally stays small:

- `planned`
- `in_progress`
- `blocked`
- `done`

Those statuses are enough to rebuild `summary`, `next`, and `snapshot` without
dragging the old compatibility runtime forward.

### `TaskClaimRecord`

The active claim over one task inside one workset.

It carries:

- task id
- actor
- execution model
- claimed timestamp
- optional attempt id
- optional note

### `TaskAttemptRecord`

One concrete execution of one task inside one workset.

It carries:

- actor identity
- prompt receipt
- workspace identity
- worktree role and worktree path
- branch intent plus observed branch and start commit
- execution model
- result summary, validations, changed paths, closure status, and commit linkage

The attempt record is where Blackdog stops being only a planner and becomes a
usable execution-memory system.

### `PromptReceiptRecord`

The structured execution input captured at attempt start.

It carries:

- prompt text
- prompt hash
- recorded timestamp
- source identifier

## Storage Model

The semantic layer works on typed objects and a store interface.
The shipped storage is JSON because it is inspectable, stdlib-friendly, and
good enough for the current scope.

BSON was rejected for this sweep because it would add binary opacity without a
defensible win over JSON for the current repo-local use case.

## Breaking Removals

These concepts were intentionally removed instead of preserved:

- markdown fenced `backlog-task` / `backlog-plan` parsing
- `backlog.md` as semantic truth
- durable `epic`, `lane`, and `wave`
- compatibility logic whose only purpose was to keep that model alive

The words `epic`, `lane`, and `wave` may still appear in historical docs or old
code, but they are removed from the vNext contract.

## Minimum Product Outcome

The minimum coherent shipped slice after the sweep is:

- one write surface for workset/task state: `blackdog workset put`
- one supervisor/workset-manager surface family: `blackdog supervisor start|show|checkpoint|release`
- one same-thread task-begin surface: `blackdog task begin`
- one same-thread task inspection surface: `blackdog task show`
- one same-thread task success-closure surface: `blackdog task land`
- one same-thread task non-success closure surface: `blackdog task close`
- one same-thread task cleanup surface: `blackdog task cleanup`
- one WTAM contract/readiness surface: `blackdog worktree preflight`
- one WTAM execution start surface: `blackdog worktree start`
- one WTAM inspection/recovery read surface: `blackdog worktree show`
- one WTAM success-closure surface: `blackdog worktree land`
- one WTAM non-success closure surface: `blackdog worktree close`
- one WTAM cleanup fallback surface: `blackdog task cleanup` (`worktree cleanup` remains a low-level alias)
- one summary surface: `blackdog summary`
- one machine snapshot surface: `blackdog snapshot`
- one workset-scoped execution-facing read surface: `blackdog next --workset`

That slice is intentionally smaller than the previous Blackdog surface area.
It is enough to prove the new foundation while keeping the package boundaries
clean.
