---
name: blackdog
description: "Use the project-local Blackdog backlog contract for Blackdog. Trigger this skill when reviewing, claiming, completing, supervising, or reporting backlog work in this repo, or when checking inbox messages and structured task results."
---

# Blackdog

Use the local Blackdog CLI instead of mutating backlog state by hand.

## CLI Entry Points

- Blackdog CLI: `blackdog`
- Skill refresh CLI: `blackdog-skill`

## Core Paths

- Profile: `/Users/bullard/Work/.worktrees/wt-black-9d0c78051f-remove-utter-residue-and-keep-blackdog-dependency-light/blackdog.toml`
- Control root: `/Users/bullard/Work/Blackdog/.git/blackdog`
- Backlog: `/Users/bullard/Work/Blackdog/.git/blackdog/backlog.md`
- State: `/Users/bullard/Work/Blackdog/.git/blackdog/backlog-state.json`
- Events: `/Users/bullard/Work/Blackdog/.git/blackdog/events.jsonl`
- Inbox: `/Users/bullard/Work/Blackdog/.git/blackdog/inbox.jsonl`
- Results: `/Users/bullard/Work/Blackdog/.git/blackdog/task-results`
- HTML view: `/Users/bullard/Work/Blackdog/.git/blackdog/backlog-index.html`

## Standard Flow

1. Run `blackdog validate`.
2. Run `blackdog summary`.
3. Inspect runnable work with `blackdog next`.
4. Before any repo edit you intend to keep, run `blackdog worktree preflight`. If it reports `primary worktree: yes`, do not edit in that checkout; create or enter a branch-backed task worktree with `blackdog worktree start --id TASK` first. Analysis-only work can stay in the current checkout.
5. Claim one task with `blackdog claim --agent <agent-name>`, then record structured output with `blackdog result record ...`.
6. Complete or release the task through the CLI for direct work.
7. Use `blackdog supervise run` when you want Blackdog to launch child agents instead of editing directly.
8. Check `blackdog inbox list --recipient <agent-name>` before claiming fresh work if the run may have pending instructions.
9. Open `/Users/bullard/Work/Blackdog/.git/blackdog/backlog-index.html` directly when you want the static backlog board; `blackdog render` refreshes it and active supervisor runs rerender it after task-state changes, including run exit after landed updates.

## Static Board

- `/Users/bullard/Work/Blackdog/.git/blackdog/backlog-index.html` renders a wide control board with a `Backlog Control` panel, `Status` panel, paired objective/release-gate tables, `Execution Map`, and `Completed Tasks`.
- The control panel shows the current push copy, branch/commit/run/time-on-task summary, progress bar, and plain artifact links.
- The release-gates panel stays beside the objective table and shows explicit or inferred passed checks without making the rows interactive.
- The execution map keeps only live lanes and waves visible, carries the `Inbox JSON` link, and removes search/filter chrome.
- Objective rows are summary-only, while execution-map and completed-task cards open the task reader popout. Completed history is grouped by sweep when run metadata exists.

## Docs to Review

Review these repo docs before editing when they apply:
  - `AGENTS.md`
  - `docs/INDEX.md`
  - `docs/ARCHITECTURE.md`
  - `docs/CLI.md`
  - `docs/FILE_FORMATS.md`

Keep `blackdog.toml` `[taxonomy].doc_routing_defaults` aligned with the repo's required review set, then regenerate this skill after routing changes.

## Supervisor Model

- The coordinating agent stays in the primary worktree.
- Child agents launched by `blackdog supervise ...` run in branch-backed task worktrees and land through the primary worktree after successful commits.
- Blackdog uses branch-backed task worktrees for kept implementation changes.
- `stop` messages are checked while a supervisor run is active. They prevent new launches, but they do not interrupt an already-running child claim.
- Tasks completed during the active run stay visible in the execution map until the next run starts and performs its opening sweep.

## Repo Contract

- Commit `blackdog.toml` and this project-local skill if the repo wants a shared Blackdog operating contract.
- Do not check in mutable runtime files from `/Users/bullard/Work/Blackdog/.git/blackdog`.
- Regenerate this skill after profile changes with `blackdog-skill refresh backlog --project-root /Users/bullard/Work/.worktrees/wt-black-9d0c78051f-remove-utter-residue-and-keep-blackdog-dependency-light`.

## Repo Defaults

- Id prefix: `BLACK`
- Buckets: core, cli, html, skills, docs, testing, integration
- Domains: cli, docs, html, state, events, inbox, results, skills
- Validation defaults:
  - `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`
