# Blackdog Charter

Blackdog exists to make AI work in a git repo durable, inspectable, and
steerable through typed repo-local state.

For the concrete v1 product target and supported workflows, use
[docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md). This document is the intent
statement for the repo.

## Product Intent

Blackdog is not a human-authored markdown backlog. It is a machine-native
workset/task runtime where:

- humans define goals, provide approvals, and inspect results
- agents shape work into worksets and tasks
- kept changes run through WTAM branch-backed worktrees
- prompt receipts, execution lineage, landed history, and runtime evidence stay
  in durable repo-local artifacts

## Core Principles

- AI-first: the primary author of planning/runtime state is the agent through
  typed operations.
- WTAM-first: kept implementation work is always worktree-backed.
- Local-first: the repo owns its contract, and mutable runtime state lives in a
  shared control root across worktrees.
- Typed over textual: semantics live in `planning.json`, `runtime.json`, and
  `events.jsonl`, not markdown surgery.
- Explicit history: completed and landed work should accumulate as durable
  attempts and events instead of being flattened into only current status.

## Current Release Line

The current release line intentionally focuses on a narrow base layer:

- typed workset/task planning
- workset and task claims
- WTAM direct execution
- durable attempt history and snapshots

`workset_manager` is now a first-class shipped execution model, rebuilt from
this base instead of preserved from the old supervisor system.
