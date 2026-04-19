# Repo Lifecycle MVP Note

This historical note captures the plan for Blackdog's second workflow family:
repo lifecycle workflows.

The repo lifecycle surfaces described here now ship, so this document is
background/reference only. Use `docs/PRODUCT_SPEC.md`,
`docs/ARCHITECTURE.md`, and `docs/CLI.md` for the current contract.

These workflows are first-class product behavior, but they are not workset or
task semantics. They belong in `blackdog` and should be surfaced through
explicit CLI and skill workflows.

## Original Goal

Make Blackdog usable in this repo and in other repos through a tight MVP around:

- repo install/update/refresh
- prompt/skill preview and tuning
- completed-work inspection and summaries

The implementation should stay close to the current vNext core:

- `blackdog_core`: durable planning/runtime/event contracts and read models
- `blackdog`: repo lifecycle and WTAM product workflows
- `blackdog_cli`: thin adapter only

## Original Proposed Scope

The MVP should ship one coherent repo lifecycle family with these surfaces:

### Repo Setup

- `blackdog repo analyze`
  Inspect a target repo, identify agent-instruction ambiguity, and propose a
  conversion plan before mutation.
- `blackdog repo install`
  Create or repair a repo-local `.VE`, install Blackdog into it, and write the
  minimum managed repo contract files when missing.
- `blackdog repo update`
  Reinstall or refresh a target repo from the current Blackdog checkout.
- `blackdog repo refresh`
  Regenerate repo-local skill and managed contract/scaffold surfaces without
  pretending this is task execution.

### Prompt / Skill Composition

- `blackdog prompt preview`
  Show the prompt/skill/repo-contract context Blackdog would use.
- `blackdog prompt tune`
  Rewrite or tune a request against the repo contract.

These flows should support both compact preview and expanded preview with skill
text included.

### Inspection / Reporting

- `blackdog attempts summary`
  Human summary of completed work and recent execution.
- `blackdog attempts table`
  Stable tabular view over completed attempts for inspection or export.
- optional `--json` on both

The table/summary layer should read from the typed runtime model and attempt
history, not from ad hoc text artifacts.

## Read Model Proposed For The MVP

Inspection should center on completed attempts, not just current task state.

Minimum columns for the table view:

- `workset_id`
- `task_id`
- `attempt_id`
- `status`
- `actor`
- `started_at`
- `ended_at`
- `elapsed_seconds`
- `execution_model`
- `model`
- `reasoning_effort`
- `prompt_source`
- `branch`
- `target_branch`
- `start_commit`
- `commit`
- `landed_commit`
- `prompt_hash`
- `changed_paths_count`
- `validation_summary`
- `summary`

Minimum summary slices:

- recent completed attempts
- completed counts by workset
- validation pass/fail totals
- landed vs not-landed completion totals

## Constraints This Note Was Written Against

- Do not encode repo lifecycle workflows as worksets, tasks, claims, or
  attempts.
- Do not move repo lifecycle logic into `blackdog_core`.
- Keep the repo skill thin; put lifecycle logic in CLI/library code.
- Keep storage machine-native and explicit.
- Prefer a smaller correct lifecycle family over reviving the old scaffold tree
  wholesale.

## Original Acceptance Criteria

The CLI now ships the repo lifecycle family described here. Keep the checklist
below as historical completion criteria rather than live scope.

Blackdog reaches repo lifecycle MVP when:

1. A repo without Blackdog can be installed or refreshed through one explicit
   repo workflow.
2. A human can inspect a target repo and receive a conversion plan before
   install.
3. The repo-local `$blackdog` skill can be regenerated through product code.
4. A human can preview and tune prompt/skill composition without starting task
   execution.
5. A human can inspect completed work through both summary and table surfaces.
6. The Blackdog repo can dogfood those flows on itself.
7. At least one other repo can dogfood those flows successfully.

## Current Source Of Truth

The repo lifecycle family described here is now part of the shipped v3 surface:

- `blackdog repo analyze|install|update|refresh`
- `blackdog prompt preview|tune`
- `blackdog attempts summary|table`

Do not use this note as an implementation prompt or a second contract. For
active behavior and next steps, use the routed docs instead.
