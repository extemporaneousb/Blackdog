# Single-Agent WTAM Audit

This document records the current single-agent Blackdog workflow as exercised
through the shipped CLI on April 13, 2026.

The goal is to freeze the recovery and assessment contract for one claimed
workset/task flow before Blackdog grows a multi-agent `workset_manager` mode.

## Normative Single-Agent Flow

1. `./.VE/bin/blackdog summary --project-root .`
2. `./.VE/bin/blackdog next --project-root . --workset WORKSET`
3. `./.VE/bin/blackdog worktree preflight --project-root .`
4. `./.VE/bin/blackdog worktree preview --project-root . --workset WORKSET --task TASK --actor AGENT --prompt "..."`
5. `./.VE/bin/blackdog worktree start --project-root . --workset WORKSET --task TASK --actor AGENT --prompt "..."`
6. make kept changes only inside that task worktree
7. `./.VE/bin/blackdog worktree land --project-root . --workset WORKSET --task TASK --actor AGENT --summary "..."`
8. if recovery is needed, inspect `./.VE/bin/blackdog worktree show --project-root . --workset WORKSET --task TASK`
9. if the work should not land, close it with `./.VE/bin/blackdog worktree close --project-root . --workset WORKSET --task TASK --actor AGENT --status blocked|failed|abandoned --summary "..."`
10. use `./.VE/bin/blackdog worktree cleanup --project-root . --workset WORKSET --task TASK` only for retained or leftover task worktrees
11. inspect `summary`, `snapshot`, `attempts summary`, and `attempts table`

The direct-agent hot path is workset-scoped and task-specific. Once the agent
already knows the workset and task, `next` is no longer the critical path; the
critical path is `preflight -> preview -> start -> land`. `show`, `close`, and
`cleanup` are the recovery/fallback surfaces around that canonical path.

## What The Current CLI Assesses Well

- `worktree preflight`
  Shows whether the current checkout is the primary worktree, whether the
  primary worktree is dirty, which worktrees exist, and whether the current
  checkout has a local `.VE` and `blackdog` launcher.
- `worktree preview`
  Shows branch/base/target details, prompt receipt metadata, routed contract
  docs, and the ordered handler plan for the task worktree. This is the main
  start-readiness surface.
- `worktree show`
  Shows one active or latest attempt with task-worktree dirtiness, branch
  status, primary-worktree dirtiness, and recommended next actions. This is
  the main focused recovery read surface.
- `summary --workset`
  Shows claimed workset/task state and recent attempts for one workset.
- `snapshot --workset`
  Shows machine-readable claims, attempts, event history, prompt receipt text,
  and handler actions.
- `attempts summary` and `attempts table`
  Surface landed/completed history for tuning and audit. This is the main
  stats surface today.

## Current Attempt Stats Surface

The shipped attempt table currently exposes enough data to audit one kept
change run:

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

That is sufficient for prompt tuning, git audit, and coarse runtime review.

## Current Landing Contract

The canonical kept-change success path is now:

- start one WTAM attempt through `blackdog worktree start`
- do the kept work only inside that task worktree
- finish with `blackdog worktree land`

`worktree land` owns the success closure contract. For a successful task
attempt it:

- creates one canonical Blackdog landed commit
- records result stats, validation outcomes, and commit lineage
- releases the active task/workset claims
- removes the task worktree and deletes its branch by default

If the task worktree still has uncommitted changes, `worktree land` stages them
and creates an internal prep commit before synthesizing the canonical landed
commit. The operator does not need a separate success-commit command.

Operational landing failures still block the success path, but they do not
leave the attempt active. `worktree land` closes the attempt as `blocked`,
records the end time, releases claims, and returns a non-zero exit code. The
most common blocking case is still a dirty primary worktree.

The earlier env-handler sweep did not produce attempt history because it was
implemented in a manual branch worktree and merged directly. That is precisely
the kind of mixed operating mode Blackdog should avoid when dogfooding itself.

## Failure Modes And Recovery

### 1. Primary worktree is dirty before `land`

Symptoms:

- `worktree land` returns non-zero and reports a dirty-primary-worktree error
- `summary --workset` no longer shows an active claim for that task

Assessment:

- `blackdog worktree show --project-root . --workset WORKSET --task TASK`
- `git status --short` in the primary worktree

Recovery:

- review and either land or discard the unrelated primary-worktree changes
- do not use stash as an implicit recovery mechanism
- start a fresh attempt and rerun `worktree land` when the primary worktree is
  clean again

### 2. Repo-root `.VE` is missing or stale before `start`

Symptoms:

- `worktree preview` shows `start_ready = false`
- handler actions show blocked root-env validation

Assessment:

- `blackdog worktree preview ... --json`
- `blackdog repo update --json`

Recovery:

- run `blackdog repo install` or `blackdog repo update`
- rerun `worktree preview`

### 3. Managed source checkout is missing in a host repo

Symptoms:

- `worktree preview` blocks on source resolution

Assessment:

- `blackdog worktree preview ... --json`
- `blackdog repo update --json`

Recovery:

- run `blackdog repo install` or `blackdog repo update`
- if a local Blackdog checkout should be authoritative, rerun with
  `--source-root /path/to/blackdog`

### 4. Task worktree exists but runtime is not cleanly closed

Symptoms:

- `summary --workset` shows a claimed workset/task or an active attempt
- `worktree cleanup` may refuse to remove a dirty worktree

Assessment:

- `blackdog worktree show --project-root . --workset WORKSET --task TASK`
- `blackdog summary --project-root . --workset WORKSET`
- `git status --short` in the task worktree

Recovery:

- if the work is valid, run `worktree land`
- if the work should not land, run `worktree close --status blocked|failed|abandoned`
- if the worktree was retained or left dirty, run `worktree cleanup` after the
  branch is no longer needed

### 5. Planning state is stale relative to what actually landed

Symptoms:

- `summary` or `next` still points at work that has already shipped

Assessment:

- `blackdog summary`
- `blackdog attempts summary`

Recovery:

- update task runtime state through `workset put` runtime patching so the
  planning/runtime read model reflects reality

## Gaps Before Multi-Agent Work

The single-agent base is much better, but it is not complete.

The biggest remaining gaps are:

- no explicit stale-claim recovery command
- no dedicated recovery command that answers "what should I do with this dirty
  task worktree right now?" across multiple stale or conflicting attempts
- no benchmark history persistence; the current timing harness is file-based
  and ad hoc

## Current Recommendation

Do not start the supervisor rebuild yet.

The next single-agent slices should focus on:

1. clearer stale-claim and dirty-worktree remediation
2. continued audit of landed attempt stats and prompt lineage
3. benchmark history persistence if the current file-based timing harness
   becomes a real operating dependency

Only after those are coherent should Blackdog treat `workset_manager` as a
serious operator surface.
