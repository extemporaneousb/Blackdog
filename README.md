# Blackdog

Blackdog is a repo-versioned backlog runtime for AI-assisted software work and a foundation for local multi-agent development supervision.

It keeps backlog semantics, state transitions, event history, structured task results, inbox messages, and status views inside the project repo instead of hiding them inside global skills or ad hoc scripts.

## Current status

Implemented today:

- Repo-local backlog parsing, validation, claims, approvals, inbox messaging, task results, and HTML rendering
- Plan structure with epics, lanes, and waves encoded in the backlog file
- A one-command repo bootstrap that creates the local profile, artifact set, and project skill
- Project-local skill scaffolding for host repositories
- A WTAM-style branch-backed worktree lifecycle for implementation tasks
- An initial supervisor runner that launches child commands against runnable tasks
- An initial persistent supervisor loop that can keep cycling, refresh repo-local status views, and honor inbox `pause` or `stop` control messages
- A served readonly live UI with a canonical snapshot contract and SSE updates driven by Blackdog state changes

Planned but not implemented yet:

- Interactive drift assessment and backlog steering during active runs
- A write-enabled runtime UI for approvals or steering from the browser itself
- A packaging/distribution path that removes the need for a preinstalled local Python environment in host repos

## What it provides

- `blackdog`: core CLI for repo bootstrap, backlog parsing, validation, task selection, claims, approvals, completion, inbox messaging, task results, HTML rendering, and the live UI server
- `blackdog-skill`: compatibility wrapper for project-specific skill generation
- `blackdog.toml`: repo-local profile for id prefixes, bucket/domain taxonomy, defaults, and heuristics
- shared runtime state under the git control root, which defaults to `@git-common/blackdog`
- `blackdog worktree ...`: explicit branch-backed worktree start/land/cleanup entrypoints for implementation work

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
blackdog bootstrap --project-name "My Project"
blackdog add \
  --title "Create first task" \
  --bucket core \
  --why "We need a real task in the queue." \
  --evidence "The backlog starts empty." \
  --safe-first-slice "Add one narrowly scoped task with a lane and render the HTML view."
blackdog summary
blackdog worktree preflight
blackdog render
blackdog ui serve --open-browser
```

## Using Blackdog In This Repo

Blackdog should use its own runtime as the default coordination contract in this repository.

For direct slices:

1. Run `blackdog validate`, `blackdog summary`, and `blackdog next`.
2. If the task will edit repo files, run `blackdog worktree preflight` and then `blackdog worktree start --id TASK`.
3. Claim the task with `blackdog claim --agent codex --id TASK`.
4. Make the change inside the task worktree, then record `blackdog result record --id TASK --actor codex ...`.
5. Finish with `blackdog complete` or `blackdog release`, then land with `blackdog worktree land --branch agent/... --cleanup`.

For delegated slices:

1. Launch `blackdog supervise run --id TASK`.
2. Launch `blackdog ui serve --open-browser` for the live readonly monitor.
3. Inspect the resolved control-root artifacts from `blackdog.toml` as the run proceeds.
4. Treat blocked child runs as product evidence and convert the gap into backlog follow-up work instead of deleting the failed artifacts.

Blackdog now treats branch-backed worktrees as the implementation default, but the supervisor still has its own child-workspace path today. Moving delegated child runs onto the same `blackdog worktree` lifecycle is tracked as follow-up work rather than implied by the current runtime.

Mutable runtime state now lives under one shared local control root across worktrees rather than as checked-in repo artifacts. In this repo, the default resolved location is `.git/blackdog/`.

Current dogfood evidence lives under the resolved control root in `supervisor-runs/` and `task-results/`.

## Design goals

- Keep the backlog runtime versioned with the repo that depends on it.
- Make skills thin adapters around a real CLI and real file formats.
- Preserve human-readable backlog markdown while moving execution semantics into structured state and event files.
- Support AI agents with explicit claims, messages, structured results, and predictable file layouts.

See [docs/INDEX.md](/Users/bullard/Work/Blackdog/docs/INDEX.md) for the full document map.
See [docs/CHARTER.md](/Users/bullard/Work/Blackdog/docs/CHARTER.md) for the product charter and [docs/INTEGRATION.md](/Users/bullard/Work/Blackdog/docs/INTEGRATION.md) for host-repo setup guidance.
