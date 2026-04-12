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

Blackdog also has a separate repo lifecycle concern set: install/update/refresh
and prompt/skill composition. Those workflows belong in the product layer, but
they are not workset/task operations.

## Docs

- [docs/INDEX.md](docs/INDEX.md)
- [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/TARGET_MODEL.md](docs/TARGET_MODEL.md)
- [docs/CLI.md](docs/CLI.md)
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md)
