---
name: blackdog
description: "Use the project-local Blackdog backlog contract for Blackdog. Trigger this skill when reviewing, claiming, completing, supervising, or reporting backlog work in this repo, or when checking inbox messages and structured task results."
---

# Blackdog

Use the local Blackdog CLI instead of mutating backlog state by hand.

## Core Paths

- Profile: `/Users/bullard/Work/Blackdog/blackdog.toml`
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
4. For direct implementation work, run `blackdog worktree preflight` and `blackdog worktree start --id TASK` before editing repo files.
5. Claim one task with `blackdog claim --agent <agent-name>`, then record structured output with `blackdog result record ...`.
6. Complete or release the task through the CLI for direct work.
7. Use `blackdog supervise run` or `blackdog supervise loop` when you want Blackdog to launch child agents instead of editing directly.
8. Check `blackdog inbox list --recipient <agent-name>` before claiming fresh work if the run may have pending instructions.

## Supervisor Model

- The coordinating agent stays in the primary worktree.
- Child agents launched by `blackdog supervise ...` run in branch-backed task worktrees and land through the primary worktree after successful commits.
- `pause` and `stop` messages are checked between loop cycles. They do not interrupt an already-running child claim.

## Repo Contract

- Commit `blackdog.toml` and this project-local skill if the repo wants a shared Blackdog operating contract.
- Do not check in mutable runtime files from `/Users/bullard/Work/Blackdog/.git/blackdog`.
- Regenerate this skill after profile changes with `blackdog-skill refresh backlog --project-root /Users/bullard/Work/Blackdog`.

## Repo Defaults

- Id prefix: `BLACK`
- Buckets: core, cli, html, skills, docs, testing, integration
- Domains: cli, docs, html, state, events, inbox, results, skills
- Validation defaults:
  - `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`
