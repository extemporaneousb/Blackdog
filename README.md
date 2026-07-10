# Blackdog

Blackdog is a machine-native task and attempt runtime for AI-driven local
development. It gives agents a WTAM kept-change workflow, durable attempt
history, repo-local setup receipts, and read models for status, recovery, and
Codex-session coverage.

## Packages

- `blackdog_core`: durable profile, planning/runtime contracts, typed
  semantics, and derived read models.
- `blackdog`: product-layer WTAM orchestration and repo lifecycle workflows on
  top of the core contract.
- `blackdog_cli`: thin parser/help/dispatch layer for the `blackdog`
  executable.

## Repo Use

In this repo, use `./.VE/bin/blackdog` when the worktree has a local `.VE`.
Before kept implementation edits, run:

```bash
./.VE/bin/blackdog worktree preflight --project-root .
```

Implementation edits belong in `workspace role: task`. From the primary or a
normal linked worktree, start a task with `blackdog task begin`, make changes
only in the returned task workspace, validate, then close with
`blackdog task land` or `blackdog task close`.

## Install And Layering Model

Blackdog installs a thin repo-local contract into target repos:

- `blackdog.toml` is the machine-readable source of truth for control paths,
  routed docs, validation commands, and runtime handlers.
- `AGENTS.md` keeps repo-owned instructions outside a managed Blackdog
  contract block.
- `.codex/skills/<repo-slug>/SKILL.md` is a generated, thin user workflow
  overlay that delegates state, setup, recovery, and landing to the CLI.
- `.VE/bin/blackdog` is the repo-local launcher.

Repo-root `.VE` is the base runtime for that checkout. Each task worktree gets
its own `.VE`; the Python handler wires the repo-root package overlay and
fallback tool scripts into the task worktree. The Blackdog runtime handler
resolves the source layer:

- In Blackdog itself, task worktrees use the current worktree source so changes
  are exercised before landing.
- In target repos, the default install uses a managed Blackdog source checkout
  under the Git common control root and writes a launcher shim into the
  target repo.
- `--source-root /path/to/blackdog` is the explicit local override for testing
  or development.

## Main Commands

- `blackdog repo analyze|bind|table|scaffold|install|update|refresh|archive|unarchive|unbind`
- `blackdog local-repo add|list|remove`
- `blackdog prompt preview|tune`
- `blackdog attempts summary|table`
- `blackdog codex coverage|history`
- `blackdog stats`
- `blackdog task begin|show|recover|land|close|cancel|reopen|cleanup`
- `blackdog worktree preflight|table|preview|start|show|land|close|cleanup`
- `blackdog summary`
- `blackdog snapshot`

## Validation

```bash
make test
make repo-acceptance
```

`make repo-acceptance` checks the install/refresh/analyze path for a target
repo and verifies that generated AGENTS/skill layering remains lean.

## Docs

- [docs/INDEX.md](docs/INDEX.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/CLI.md](docs/CLI.md)
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md)
