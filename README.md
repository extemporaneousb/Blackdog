# Blackdog

Blackdog is a repo-versioned backlog system for AI-assisted local
development.

It keeps the durable backlog/runtime contract in repo-local code and a
shared local control root instead of hiding execution state inside
global skills, ad hoc scripts, or one editor integration.

Blackdog is being reshaped around a fixed three-layer boundary:

- `blackdog.core`: durable backlog/runtime primitives, canonical file
  contracts, deterministic state transitions, and WTAM safety facts
- `blackdog.proper`: the shipped Blackdog product surface built on top
  of `core`
- `extensions`: optional adapters such as the Emacs workbench

The important split is that `core` is the narrow reusable runtime
contract. Supervisor behavior, HTML rendering, bootstrap flows, and
generated skills are product surfaces layered on top of it, not the
runtime itself.

Use these docs for the detailed contract:

- [docs/BOUNDARIES.md](docs/BOUNDARIES.md): ownership rules and
  extraction phases
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how `core`,
  `blackdog proper`, and `extensions` compose
- [docs/CLI.md](docs/CLI.md): command ownership and command-level
  behavior
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md): canonical runtime
  artifacts and schemas

## Current status

Implemented today:

- `blackdog.core` already owns the repo profile, backlog parsing,
  deterministic backlog/runtime state, canonical artifact formats, and
  the WTAM inspection contract the rest of the system uses
- `blackdog.proper` already ships the CLI, bootstrap/refresh flows,
  project-local skill generation, branch-backed worktree lifecycle,
  supervisor runner, snapshot/render pipeline, and static HTML board
- `extensions` already include the local Emacs workbench as an optional
  operator surface rather than part of the minimal runtime

Still in progress:

- finishing the extraction so current mixed module placement matches
  the documented `core` versus `blackdog proper` ownership split
- hardening delegated supervisor behavior without making it the only
  way to operate Blackdog on Blackdog
- improving packaging and host-repo adoption without weakening the
  repo-local contract

## What it provides

- `blackdog`: the main product CLI
- `blackdog-skill`: compatibility wrapper for project-local skill
  generation
- `blackdog.toml`: repo-local profile and path entrypoint
- a shared control-root runtime state layout, which defaults to
  `@git-common/blackdog`
- `blackdog worktree ...`: WTAM-oriented implementation workflow
- `.codex/skills/<skill-name>/`: generated project-local skill
  scaffold for host repos
- optional extension surfaces such as `editors/emacs/`

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

- `.codex/skills/<skill-name>/SKILL.md`
- `.codex/skills/<skill-name>/agents/openai.yaml`

By default `<skill-name>` is `blackdog-<project-slug>`, so each host repo gets its own wrapper skill token.
Codex surfaces that project-local token from `agents/openai.yaml` once these files exist and the repo is opened in Codex with file-system-based skill discovery enabled (reopen the repo if the skill list was already loaded).

Then continue with the usual contract commands:

```bash
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

Blackdog should use its own runtime as the default coordination
contract in this repository.

In this repo, invoke Blackdog through `./.VE/bin/blackdog` and
`./.VE/bin/blackdog-skill` unless the current shell has already
activated `.VE`.

For direct slices, the repo stays manual-first:

1. Run `./.VE/bin/blackdog validate`, `./.VE/bin/blackdog summary`, and `./.VE/bin/blackdog next`.
2. If the task will edit repo files, run `./.VE/bin/blackdog worktree preflight` and then `./.VE/bin/blackdog worktree start --id TASK`.
3. Claim the task with `./.VE/bin/blackdog claim --agent codex --id TASK`.
4. Make the change inside the task worktree, then record `./.VE/bin/blackdog result record --id TASK --actor codex ...`.
5. Finish with `./.VE/bin/blackdog complete` or `./.VE/bin/blackdog release`, then land with `./.VE/bin/blackdog worktree land --branch agent/... --cleanup`.

For delegated slices:

1. Launch `./.VE/bin/blackdog supervise run ...` when you are explicitly exercising delegated execution or supervisor behavior.
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
- Keep `blackdog.core` narrow enough that other layers can depend on it without inheriting Blackdog-product policy.
- Make skills thin adapters around a real CLI and real file formats.
- Preserve human-readable backlog markdown while moving execution semantics into structured state and event files.
- Support AI agents with explicit claims, messages, structured results, and predictable file layouts.

See [docs/INDEX.md](docs/INDEX.md) for the full document map.
See [docs/CHARTER.md](docs/CHARTER.md) for the product charter and [docs/INTEGRATION.md](docs/INTEGRATION.md) for host-repo setup guidance.
See [docs/BOUNDARIES.md](docs/BOUNDARIES.md) for the frozen split between `core`, `blackdog proper`, and `extensions`.
See [docs/EMACS.md](docs/EMACS.md) for the local Emacs 30+ workbench architecture, dependency tiers, keybindings, installation, workflows, and packaging notes.
