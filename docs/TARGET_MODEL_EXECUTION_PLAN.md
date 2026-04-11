# Target Model Execution Plan

This document turns [docs/TARGET_MODEL.md](docs/TARGET_MODEL.md) into a compatibility-first implementation program for the current Blackdog codebase.

The goal is not a destructive rewrite. The goal is to land the new model through explicit slices that preserve the current runtime while introducing the new vocabulary, lineage, and control-plane objects.

## Program Constraints

These constraints apply to every slice:

- `blackdog_core` remains the durable runtime kernel. It may own typed models, durable artifacts, normalizers, and derived read models. It must not absorb WTAM orchestration, supervisor policy, CLI glue, or viewer composition.
- `blackdog` remains the orchestration/product layer. It may own worktree lifecycle, supervisor behavior, prompt shaping, launch policy, and runtime coordination over the core artifacts.
- `blackdog_cli` stays thin. New commands or flags must delegate into core or product code rather than owning semantics.
- Mutable runtime state stays outside git history under the shared control root.
- Legacy `epic` / `lane` / `wave` and `run` surfaces remain readable during migration, but they become compatibility projections rather than the preferred model.
- Full prompt receipts become a completion invariant for new executions.
- Every new durable object must have a clear durable-vs-derived story before it lands.

## Completion Invariants

The implementation is only complete when all of these are true:

- Blackdog has first-class typed models for `Repository`, `Workspace`, `Workset`, `TaskState`, `TaskAttempt`, `WorksetExecution`, `PromptReceipt`, `WaitCondition`, `ControlMessage`, `Result`, and `Event`.
- The runtime can project a `Workset` plus task-DAG view from current backlog data without breaking existing backlog files.
- Every new execution records a stable `attempt_id` and a full prompt receipt.
- Supervisor status can explain what is active, blocked, and waiting without inferring everything from freeform logs.
- Current CLI, snapshot, board, and file-format surfaces remain readable while exposing the new projections.
- Contract tests pin the architecture boundaries and the new vocabulary so the model cannot drift back silently.

## Slice Plan

### Slice 1: Editorial Freeze And Program Wiring

Owner: local orchestrator

Scope:

- finish the editorial pass on `docs/TARGET_MODEL.md`
- create this execution plan
- keep the package-boundary story explicit in docs and tests

Acceptance criteria:

- the target-model doc is concise, boundary-aware, and free of the removed MCP/tool-survey material
- this plan names the slices, owners, invariants, and validation strategy

### Slice 2: Core Runtime Model Layer

Owner: worker, `blackdog_core` only

Write scope:

- `src/blackdog_core/runtime_model.py` or equivalent new core module
- `src/blackdog_core/snapshot.py`
- focused core tests

Scope:

- add typed read models for repository, workspace, workset, task state, task attempt, prompt receipt, wait condition, control message, result, and event
- build compatibility projections from current artifacts

Acceptance criteria:

- existing backlog/state/event/result artifacts still load without migration
- core models are stdlib-only and do not import `blackdog`
- `runtime_snapshot` can expose the new projections without removing legacy fields

### Slice 3: Attempt And Prompt Lineage

Owner: worker, supervisor/state slice

Write scope:

- `src/blackdog_core/state.py`
- `src/blackdog/supervisor.py`
- focused CLI/runtime tests

Scope:

- introduce durable `attempt_id` handling for new executions
- persist full prompt receipts and link them to attempt, workspace, branch, result, and commit state
- keep legacy `run_id` readable as compatibility lineage

Acceptance criteria:

- every new supervised execution records one attempt id and one full prompt receipt
- result records and events can be correlated back to that attempt
- legacy result/event validation still passes

### Slice 4: Typed Control And Wait State

Owner: worker, supervisor/control slice

Write scope:

- `src/blackdog_core/state.py`
- `src/blackdog/supervisor.py`
- focused CLI/runtime tests

Scope:

- introduce typed control-message and wait-condition models
- keep inbox replay compatible with existing `message` / `resolve` rows
- surface typed control and wait data in supervisor status/report paths

Acceptance criteria:

- `supervise status` can explain active waits and typed control state directly
- restart/reload can reconstruct current wait/control state from durable artifacts
- old inbox commands still function

### Slice 5: Workset And Planning Projection

Owner: local orchestrator

Write scope:

- `src/blackdog_core/backlog.py`
- `src/blackdog_core/snapshot.py`
- `src/blackdog_cli/main.py`
- `src/blackdog/board.py`
- compatibility tests

Scope:

- introduce `Workset` as the preferred planning container
- derive a task DAG from current predecessor/task ordering
- keep `epic` / `lane` / `wave` readable as legacy projections during migration
- expose workset-scoped views so unrelated work can be hidden by default

Acceptance criteria:

- current backlog files still round-trip
- new workset projections agree with old task ordering/blocking for current data
- snapshot/board/CLI surfaces expose workset-aware data without breaking old callers

### Slice 6: Docs, Contracts, And Compatibility Sweep

Owner: worker, docs/tests slice

Write scope:

- docs that still teach `epic` / `lane` / `wave` as normative
- contract tests and CLI tests

Scope:

- align docs with the compatibility-first migration story
- mark legacy vocabulary as compatibility-only where appropriate
- freeze new architecture and vocabulary expectations in tests

Acceptance criteria:

- docs consistently present `Workset`, `TaskAttempt`, and `WorksetExecution` as the target model
- old terms are either removed from normative docs or clearly labeled as legacy projections
- test coverage catches boundary drift and vocabulary regression

## Multi-Agent Ownership

The implementation should be parallelized with disjoint write sets:

- worker A owns the new core runtime-model layer
- worker B owns attempt/prompt lineage
- worker C owns typed control/wait state and docs/test alignment where it does not overlap other workers
- the local orchestrator owns backlog/planning projection, final integration, snapshot/CLI coordination, unsticking, and final validation

Workers are not alone in the codebase. They must not revert each other’s edits, and they should adapt to the evolving integration shape instead of assuming the workspace is static.

## Validation Matrix

Every slice should run the narrowest relevant tests first. Before landing the combined work, run:

- focused core tests for new runtime-model and backlog/state projections
- focused supervisor/CLI tests for prompt receipts, control messages, waits, and snapshot status
- `make test`

## Landing Rule

Do not land a partial architectural rename that leaves the repo split across old and new concepts without adapters. The safe path is:

- add the new objects and projections
- adapt the product layer to write and read them
- keep legacy vocabulary readable until docs, tests, and compatibility surfaces agree on the new model
