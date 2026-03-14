---
name: blackdog-backlog
description: "Use the repo-versioned Blackdog backlog for Blackdog. Trigger this skill when preparing, reviewing, claiming, completing, or reporting backlog work in this project, or when checking inbox messages and structured task results."
---

# Blackdog Backlog

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
4. Claim one task with `blackdog claim --agent <agent-name>`.
5. Record structured output with `blackdog result record ...`.
6. Complete or release the task through the CLI.
7. Check `blackdog inbox list --recipient <agent-name>` before claiming fresh work if the run may have pending instructions.

## Interaction Model

- Use `blackdog inbox send` for user, supervisor, or child-agent instructions/questions.
- Use `blackdog comment` for task-scoped narrative notes that belong in the event log.
- Use `blackdog result record` for structured `what_changed`, `validation`, `residual`, `needs_user_input`, and `followup_candidates`.
- Use `blackdog render` whenever you need a refreshed HTML control page.

## Repo Defaults

- Id prefix: `BLACK`
- Buckets: core, cli, html, skills, docs, testing, integration
- Domains: cli, docs, html, state, events, inbox, results, skills
- Validation defaults:
  - `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`
