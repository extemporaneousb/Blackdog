# Blackdog

Blackdog is a repo-versioned backlog system for AI-assisted local
development.

It keeps the durable backlog/runtime contract in repo-local code and a
shared local control root instead of hiding execution state inside
global skills, ad hoc scripts, or one editor integration.

Blackdog is being reshaped around a fixed three-layer boundary:

- `blackdog-core` / `blackdog_core`: the durable library contract
- `blackdog` / `blackdog`: the shipped product layer built on top of
  that contract
- `blackdog-cli` / `blackdog_cli`: the thin command adapter package
- `extensions`: optional adapters such as the Emacs workbench

The important split is that `blackdog_core` is the reusable runtime
contract. Supervisor behavior, HTML rendering, bootstrap flows,
conversation threads, tracked-install registries, and generated skills
are product surfaces layered on top of it, not the runtime itself.

Use these docs for the detailed contract:

- [docs/BOUNDARIES.md](docs/BOUNDARIES.md): ownership rules and
  extraction phases
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how `blackdog_core`,
  `blackdog`, `blackdog_cli`, and `extensions` compose
- [docs/CLI.md](docs/CLI.md): command ownership and command-level
  behavior
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md): canonical runtime
  artifacts and schemas
- [docs/MIGRATION.md](docs/MIGRATION.md): migration guidance for
  callers and host repos moving onto the remodeled surface
- [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md): final remodel release
  notes and compatibility summary
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md): final acceptance checklist
  and evidence sources

## What it provides

- One executable: `blackdog`
- One command adapter package: `blackdog_cli`
- One core library surface:
  `blackdog_core.profile`, `blackdog_core.backlog`,
  `blackdog_core.state`, and `blackdog_core.snapshot`
- One product library surface:
  `blackdog.scaffold`, `blackdog.worktree`, `blackdog.supervisor`,
  `blackdog.tuning`, `blackdog.conversations`, `blackdog.installs`,
  and `blackdog.board`
- `blackdog.toml`: repo-local profile and path entrypoint
- a shared control-root runtime state layout, which defaults to
  `@git-common/blackdog`
- `blackdog worktree ...`: WTAM-oriented implementation workflow
- `.codex/skills/<skill-name>/`: generated project-local skill
  scaffold for host repos
- optional extension surfaces such as `extensions/emacs/`

## External client contract

External adapters should treat `blackdog snapshot` as a product-owned
envelope and `runtime_snapshot` as the stable shared machine contract.
Read shared backlog/runtime facts such as project identity, counts,
objectives, plan rows, open inbox rows, and durable task state from
`runtime_snapshot`; treat top-level snapshot fields like `tasks`,
`board_tasks`, `queue_status`, run metadata, and artifact hrefs as the
board/editor projection.

Use these terms consistently when programming against Blackdog:

- `State`: the current mutable authority from `backlog-state.json`;
  today that is approval and claim state only
- `Record`: a stored append-only artifact such as an event, inbox
  entry, or task-result file
- `Plan`: the backlog execution intent structure made of epics, lanes,
  waves, and task membership
- `Snapshot`: a derived read-only projection such as
  `BacklogSnapshot`, `runtime_snapshot`, or the product-owned board
  snapshot

Minimal example:

```bash
blackdog snapshot | jq '.runtime_snapshot.tasks[] | {id, title, claim_status, latest_result_status}'
```

Legacy `blackdog thread ...` commands manage Blackdog-owned prompt/task
threads. Client-native chat/session storage such as Codex transcripts is
separate from that thread artifact surface.

## Quick start

```bash
python3 -m venv .VE
source .VE/bin/activate
python -m pip install -e .
```

That install exposes the `blackdog` executable and the import packages
`blackdog_core`, `blackdog`, and `blackdog_cli`. The long-term
distribution split is `blackdog-core`, `blackdog`, and `blackdog-cli`.

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

In this repo, invoke Blackdog through `./.VE/bin/blackdog` unless the
current shell has already activated `.VE`.

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

Use `make acceptance` for the repo-level closeout validation pass. That
alias runs `make test` and `make test-emacs` so the Python and Emacs
surfaces prove the same remodeled contract.

This repo uses a top-level `.VE/` virtual environment for local Blackdog development. Recreate it with `python3 -m venv .VE` and install Blackdog into it with `./.VE/bin/python -m pip install -e .`. Treat `./.VE/bin/blackdog` as the canonical CLI entrypoint in this repo even if another `blackdog` is on `PATH`.

## Design goals

- Keep the Blackdog contract repo-local and versioned with the project, but keep mutable runtime state in the shared control root.
- Keep `blackdog_core` narrow enough that other layers can depend on it without inheriting Blackdog-product policy.
- Make skills thin adapters around a real CLI and real file formats.
- Preserve human-readable backlog markdown while moving execution semantics into structured state and event files.
- Support AI agents with explicit claims, messages, structured results, and predictable file layouts.

See [docs/INDEX.md](docs/INDEX.md) for the full document map.
See [docs/CHARTER.md](docs/CHARTER.md) for the product charter and [docs/INTEGRATION.md](docs/INTEGRATION.md) for host-repo setup guidance.
See [docs/BOUNDARIES.md](docs/BOUNDARIES.md) for the frozen split between `core`, `blackdog`, and `extensions`.
See [docs/EMACS.md](docs/EMACS.md) for the local Emacs 30+ workbench architecture, dependency tiers, keybindings, installation, workflows, and packaging notes.
