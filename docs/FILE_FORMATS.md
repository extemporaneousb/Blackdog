# File Formats

The vNext Blackdog contract is machine-owned and JSON-first.

By default `paths.control_dir = "@git-common/blackdog"` resolves to the shared
git common directory, so all worktrees see the same planning and runtime state.

`backlog.md` is not part of the vNext contract.
backlog.md is not part of the vNext contract.

## Canonical Files

The durable control-root files are:

- `planning.json`
- `runtime.json`
- `events.jsonl`

## `planning.json`

Canonical planning truth.

Top-level fields:

- `schema_version`
- `store_version`
- `worksets`

Current version markers:

- `schema_version = 1`
- `store_version = "blackdog.planning/vnext1"`

Each workset row contains:

- `id`
- `title`
- `scope`
- `visibility`
- `policies`
- `workspace`
- `branch_intent`
- `tasks`
- `metadata`

Each task row contains:

- `id`
- `title`
- `intent`
- optional `description`
- `depends_on`
- `paths`
- `docs`
- `checks`
- `metadata`

## `runtime.json`

Canonical mutable runtime state.

Top-level fields:

- `schema_version`
- `store_version`
- `worksets`

Current version markers:

- `schema_version = 2`
- `store_version = "blackdog.runtime/vnext2"`

Each runtime workset row contains:

- `id`
- optional `workset_claim`
- `task_claims`
- `task_states`
- `attempts`

`workset_claim`, when present, is one JSON object with:

- `actor`
- `execution_model`
- `claimed_at`
- optional `note`

Each `task_claims` row contains:

- `task_id`
- `actor`
- `execution_model`
- `claimed_at`
- optional `attempt_id`
- optional `note`

Each task-state row contains:

- `task_id`
- `status`
- optional `updated_at`
- optional `note`

Each attempt row contains:

- `attempt_id`
- `task_id`
- `status`
- `actor`
- `started_at`
- optional `ended_at`
- optional `summary`
- optional `workspace_identity`
- optional `workspace_mode`
- optional `worktree_role`
- optional `worktree_path`
- optional `branch`
- optional `target_branch`
- optional `integration_branch`
- optional `start_commit`
- optional `execution_model`
- optional `model`
- optional `reasoning_effort`
- optional `prompt_receipt`
- `changed_paths`
- `validations`
- `residuals`
- `followup_candidates`
- optional `note`
- optional `commit`
- optional `landed_commit`
- optional `elapsed_seconds`

Allowed statuses:

- `planned`
- `in_progress`
- `blocked`
- `done`

Allowed attempt statuses:

- `in_progress`
- `success`
- `blocked`
- `failed`

Allowed validation statuses:

- `passed`
- `failed`
- `skipped`

`prompt_receipt`, when present, is one JSON object with:

- `text`
- `prompt_hash`
- `recorded_at`
- optional `source`

Current shipped execution-context values:

- `execution_model = "direct_wtam" | "workset_manager"`
- `workspace_mode = "git-worktree"`
- `worktree_role = "primary" | "task" | "linked"`

## `events.jsonl`

Append-only audit log for semantic mutations.

Each row is one JSON object with:

- `event_id`
- `type`
- `at`
- `actor`
- `payload`

Current shipped write path:

- `workset.put`
- `workset.claim`
- `workset.release`
- `task.claim`
- `task.release`
- `task.start`
- `task.finish`
- `worktree.start`
- `worktree.land`
- `worktree.cleanup`

Current `worktree.start` payloads record:

- `workset_id`
- `task_id`
- `attempt_id`
- `branch`
- `target_branch`
- `base_ref`
- `base_commit`
- `worktree_path`
- `prompt_hash`
- optional `prompt_source`
- `workspace_blackdog_path`
- `bootstrap_mode`

## Semantic Boundary

`blackdog_core.backlog` works on typed `Workset` and `TaskSpec` objects plus a
planning-store interface.
`blackdog_core.state` works on typed runtime rows plus a runtime-store
interface.

That boundary exists so storage format can change later without dragging the
semantic layer back into file-specific text editing.

## Removed Format Contracts

The following are intentionally removed from the canonical contract:

- markdown `backlog-task` fence parsing
- markdown `backlog-plan` fence parsing
- durable `epic`
- durable `lane`
- durable `wave`

Those concepts may exist in old artifacts or old code, but they do not define
Blackdog vNext behavior.
