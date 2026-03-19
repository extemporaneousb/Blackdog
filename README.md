# Blackdog

Blackdog is a repo-scoped backlog runtime for AI-assisted software work and a foundation for local multi-agent development supervision.

It keeps backlog semantics, state transitions, event history, structured task results, inbox messages, and status views in repo-local code plus a shared local control root instead of hiding them inside global skills or ad hoc scripts.

## Current status

Implemented today:

- Repo-local backlog parsing, validation, claims, approvals, inbox messaging, task results, and HTML rendering
- Plan structure with epics, lanes, and waves encoded in the backlog file
- A one-command repo bootstrap that creates the local profile, control-root runtime scaffold, and project skill
- Project-local skill scaffolding for host repositories
- A WTAM branch-backed worktree lifecycle for implementation tasks
- An initial supervisor runner that launches child agents into branch-backed task worktrees and lands their commits through the primary worktree
- An initial persistent supervisor loop that can keep cycling, refresh repo-local status views, honor inbox `pause` or `stop` control messages, and reread backlog state between cycles
- A static backlog index that embeds JSON task data and links directly to task artifacts on disk

Planned but not implemented yet:

- Interactive drift assessment and backlog steering during active runs
- A write-enabled runtime UI for approvals or steering from the browser itself
- A packaging/distribution path that removes the need for a preinstalled local Python environment in host repos

## What it provides

- `blackdog`: core CLI for repo bootstrap, backlog parsing, validation, task selection, claims, approvals, completion, inbox messaging, task results, static HTML rendering, and supervisor control
- `blackdog-skill`: compatibility wrapper for project-specific skill generation
- `AGENTS.md`: baseline host-repo operating instructions generated on bootstrap when absent
- `blackdog.toml`: repo-local profile for id prefixes, bucket/domain taxonomy, defaults, and heuristics
- shared runtime state under the git control root, which defaults to `@git-common/blackdog`
- `blackdog worktree ...`: explicit branch-backed worktree start/land/cleanup entrypoints for implementation work
- `.codex/skills/blackdog/`: project-local skill scaffold that teaches agents how to use Blackdog in that repo

## Quick start

```bash
python3 -m venv .VE
source .VE/bin/activate
python -m pip install -e .
```

## Fresh host install

For the first deployment in another repository, install Blackdog into that repo's Python environment first, then bootstrap:

```bash
# Editable local checkout (recommended for internal hosting)
python3 -m pip install -e /path/to/blackdog

# Or install directly from git
python3 -m pip install git+https://github.com/<org>/blackdog.git

cd /path/to/host-repo
blackdog bootstrap --project-name "Repo Name"
```

Bootstrap writes the project-local skill discovery payload to:

- `.codex/skills/blackdog/SKILL.md`
- `.codex/skills/blackdog/agents/openai.yaml`

Codex surfaces the `blackdog` skill from `agents/openai.yaml` once these files exist and the repo is opened in Codex with file-system-based skill discovery enabled (reopen the repo if the skill list was already loaded).

Then continue with the usual contract commands:

```bash
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
open .git/blackdog/backlog-index.html
```

## Using Blackdog In This Repo

Blackdog should use its own runtime as the default coordination contract in this repository.

In this repo, invoke Blackdog through `./.VE/bin/blackdog` and `./.VE/bin/blackdog-skill` unless the current shell has already activated `.VE`.

For direct slices:

1. Run `./.VE/bin/blackdog validate`, `./.VE/bin/blackdog summary`, and `./.VE/bin/blackdog next`.
2. If the task will edit repo files, run `./.VE/bin/blackdog worktree preflight` and then `./.VE/bin/blackdog worktree start --id TASK`.
3. Claim the task with `./.VE/bin/blackdog claim --agent codex --id TASK`.
4. Make the change inside the task worktree, then record `./.VE/bin/blackdog result record --id TASK --actor codex ...`.
5. Finish with `./.VE/bin/blackdog complete` or `./.VE/bin/blackdog release`, then land with `./.VE/bin/blackdog worktree land --branch agent/... --cleanup`.

For delegated slices:

1. Launch `./.VE/bin/blackdog supervise run --id TASK` for a one-shot pass or `./.VE/bin/blackdog supervise loop` for ongoing processing.
2. Open `.git/blackdog/backlog-index.html` directly when you want the latest static task index. Reload the file after supervisor or CLI writes.
3. Keep the coordinating agent in the primary worktree. Blackdog gives each child task agent its own branch-backed task worktree and lands successful commits through the primary worktree.
4. Use inbox `pause` or `stop` messages as boundary controls between loop cycles. They do not interrupt a child task that is already running.
5. Inspect the resolved control-root artifacts from `blackdog.toml` as the run proceeds.
6. Treat blocked child runs as product evidence and convert the gap into backlog follow-up work instead of deleting the failed artifacts.

Mutable runtime state now lives under one shared local control root across worktrees rather than as checked-in repo artifacts. In this repo, the default resolved location is `.git/blackdog/`.

Current dogfood evidence lives under the resolved control root in `supervisor-runs/` and `task-results/`.

This repo uses a top-level `.VE/` virtual environment for local Blackdog development. Recreate it with `python3 -m venv .VE` and install Blackdog into it with `./.VE/bin/python -m pip install -e .`. Treat `./.VE/bin/blackdog` as the canonical CLI entrypoint in this repo even if another `blackdog` is on `PATH`.

## Design goals

- Keep the Blackdog contract repo-local and versioned with the project, but keep mutable runtime state in the shared control root.
- Make skills thin adapters around a real CLI and real file formats.
- Preserve human-readable backlog markdown while moving execution semantics into structured state and event files.
- Support AI agents with explicit claims, messages, structured results, and predictable file layouts.

See [docs/INDEX.md](docs/INDEX.md) for the full document map.
See [docs/CHARTER.md](docs/CHARTER.md) for the product charter and [docs/INTEGRATION.md](docs/INTEGRATION.md) for host-repo setup guidance.
