# Blackdog

Blackdog is a machine-native planning and runtime kernel for AI-first repo
work.

It owns a typed workset/task store under a shared control root and a WTAM
kept-change workflow on top of that store. Humans primarily author docs,
approvals, and prompts. Agents mutate planning and runtime state through the
CLI and typed runtime operations.

## Shipped Surface

The current shipped CLI is deliberately narrow:

- `blackdog init`
- `blackdog repo install`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog workset put`
- `blackdog summary`
- `blackdog next`
- `blackdog snapshot`
- `blackdog worktree preflight`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree land`
- `blackdog worktree cleanup`

Everything else from the legacy backlog/board/supervisor/bootstrap era has
been removed from the active repo surface and must be rebuilt explicitly on
top of the vNext model if it returns.

## Packages

- `blackdog_core`: durable profile, planning/runtime contracts, typed
  semantics, and derived read models
- `blackdog`: WTAM orchestration on top of the core contract
- `blackdog_cli`: thin parser/help/dispatch layer

## Repo Use

In this repo, use `./.VE/bin/blackdog` when the worktree has a local `.VE`.
Do not keep implementation edits in the primary worktree. Run
`./.VE/bin/blackdog worktree preflight` first; if it reports the primary
worktree, move into a branch-backed task worktree before editing files.
Use `./.VE/bin/blackdog worktree preview` when you want to inspect the WTAM
start plan, prompt receipt, and repo contract inputs before a claim/start.
`blackdog worktree start` provisions a worktree-local `.VE` when needed.

Blackdog has no non-WTAM implementation mode.

Blackdog also has a separate repo lifecycle concern set. Install/update/refresh,
prompt composition, and attempt inspection now ship as explicit product-layer
workflows, not workset/task operations.

For non-Blackdog repos, `blackdog repo install` defaults to a managed Blackdog
source checkout under the control root, sourced from GitHub. Use
`--source-root /path/to/blackdog` to override that with a local checkout. When
the target repo is Blackdog itself, install/update reuse that repo as the
source checkout. `blackdog repo refresh` also prunes known legacy backlog-era
artifacts from the shared control root.

## Docs

- [docs/INDEX.md](docs/INDEX.md)
- [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/TARGET_MODEL.md](docs/TARGET_MODEL.md)
- [docs/CLI.md](docs/CLI.md)
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md)
