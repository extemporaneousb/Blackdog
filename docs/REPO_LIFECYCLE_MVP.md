# Repo Lifecycle MVP

This document covers the second Blackdog workflow family: repo lifecycle
workflows.

These workflows are first-class product behavior, but they are not workset or
task semantics. They belong in `blackdog` and should be surfaced through
explicit CLI and skill workflows.

## Goal

Make Blackdog usable in this repo and in other repos through a tight MVP around:

- repo install/update/refresh
- prompt/skill preview and tuning
- completed-work inspection and summaries

The implementation should stay close to the current vNext core:

- `blackdog_core`: durable planning/runtime/event contracts and read models
- `blackdog`: repo lifecycle and WTAM product workflows
- `blackdog_cli`: thin adapter only

## MVP Scope

The MVP should ship one coherent repo lifecycle family with these surfaces:

### Repo Setup

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

## MVP Read Model

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

## MVP Rules

- Do not encode repo lifecycle workflows as worksets, tasks, claims, or
  attempts.
- Do not move repo lifecycle logic into `blackdog_core`.
- Keep the repo skill thin; put lifecycle logic in CLI/library code.
- Keep storage machine-native and explicit.
- Prefer a smaller correct lifecycle family over reviving the old scaffold tree
  wholesale.

## Acceptance Criteria

Blackdog reaches repo lifecycle MVP when:

1. A repo without Blackdog can be installed or refreshed through one explicit
   repo workflow.
2. The repo-local `$blackdog` skill can be regenerated through product code.
3. A human can preview and tune prompt/skill composition without starting task
   execution.
4. A human can inspect completed work through both summary and table surfaces.
5. The Blackdog repo can dogfood those flows on itself.
6. At least one other repo can dogfood those flows successfully.

## Continuation Prompt

Use this prompt to continue the repo lifecycle MVP work:

```text
Blackdog has two first-class workflow families:
1. workset/task execution over the typed planning/runtime model
2. repo lifecycle workflows for install/update/refresh, prompt/skill composition, and completed-work inspection

Continue Blackdog from that model.

Important constraints:
- keep `blackdog_core` limited to durable planning/runtime/event contracts and read models
- put repo lifecycle behavior in `blackdog`
- keep `blackdog_cli` thin
- do not encode repo lifecycle workflows as worksets, tasks, claims, or attempts
- do not revive the old scaffold/bootstrap/tune code tree unchanged
- keep the repo-local skill thin and generated from product behavior where appropriate

Target the repo lifecycle MVP in this order:
1. `blackdog repo install`
2. `blackdog repo update`
3. `blackdog repo refresh`
4. `blackdog prompt preview`
5. `blackdog prompt tune`
6. `blackdog attempts summary`
7. `blackdog attempts table`

Inspection requirements:
- summary and table views must be driven by typed attempt history
- include completed-work visibility that is useful for dogfooding and export
- prefer explicit columns and stable JSON over fancy rendering

Proof requirements:
- tests use fresh isolated git repos
- tests cover repo install/update/refresh flows
- tests cover prompt preview/tune behavior
- tests cover attempts summary/table output
- `make test` passes

Do not preserve old compatibility behavior unless it is clearly the fastest path
to the new lifecycle MVP.
```
