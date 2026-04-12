# AGENTS

Blackdog is a machine-native workset/task runtime for AI-driven local
development.

## Working Rules

- Keep the core dependency-light. Prefer the Python standard library unless a
  dependency is clearly justified.
- Use the current worktree's top-level `.VE` for Blackdog CLI invocations when
  it exists; prefer `./.VE/bin/blackdog` and not a different `blackdog` on
  `PATH`.
- Treat kept implementation edits in the primary worktree as a contract
  violation. Before any repo edit you intend to keep, run
  `./.VE/bin/blackdog worktree preflight`; if it reports `primary worktree:
  yes`, stop and create or enter a branch-backed task worktree before touching
  repo files.
- `.VE/` is not versioned. Each git worktree needs its own `.VE` rooted at
  that worktree; do not copy virtualenv directories between worktrees because
  they embed absolute paths.
- Blackdog uses WTAM for kept implementation changes. There is no non-WTAM
  implementation mode.
- The active shipped CLI surface is:
  - `blackdog init`
  - `blackdog workset put`
  - `blackdog summary`
  - `blackdog next`
  - `blackdog snapshot`
  - `blackdog worktree preflight|start|land|cleanup`
- Do not use or preserve deleted backlog/board/bootstrap/inbox/render/tune
  workflows unless they are explicitly rebuilt on top of the vNext core model.
- Keep `[taxonomy].doc_routing_defaults` pointed at the docs agents must review
  before editing.
- Treat the file formats in `docs/FILE_FORMATS.md` as the contract for
  planning, runtime, and event artifacts.
- Keep skills thin. If a change adds logic that belongs in the CLI or library,
  move it there instead of expanding prompt-only behavior.
- Update docs in `docs/` when CLI behavior or file formats change.

## Target Package Boundaries

- Keep `blackdog_core` limited to durable planning/runtime contracts:
  profile/path resolution, canonical planning/runtime/event formats, typed
  claim/attempt semantics, and derived read models.
- `blackdog_core` explicitly excludes WTAM orchestration, supervisor policy,
  bootstrap/refresh flows, skill generation, prompt tuning, and rendered UI
  surfaces.
- Keep `blackdog` limited to product-layer WTAM orchestration on top of the
  typed core model.
- Keep `blackdog_cli` as a thin adapter over the shipped CLI surface. No
  domain logic belongs there.
- If a change needs client-specific context to make sense, it does not belong
  in core.

## Validation

- Run `make test` after meaningful Python changes.
- Run targeted CLI smoke checks when changing workset or WTAM behavior.
