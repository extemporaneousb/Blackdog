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
  - `blackdog repo install|update|refresh`
  - `blackdog prompt preview|tune`
  - `blackdog attempts summary|table`
  - `blackdog workset put`
  - `blackdog summary`
  - `blackdog next --workset`
  - `blackdog snapshot`
  - `blackdog worktree preflight|preview|start|land|cleanup`
- Treat `blackdog next` as a workset-scoped operator/recovery surface. The
  direct-agent WTAM path usually already knows `--workset` and `--task`.
- Use `blackdog worktree preview` before `start` when you need to inspect the
  prompt receipt, repo contract inputs, branch/worktree plan, or worktree-local
  handler plan.
- `blackdog.toml` owns explicit `[[handlers]]` blocks for repo-local env and
  runtime setup. Keep env/bootstrap policy there, not in the skill.
- `blackdog worktree start` is responsible for executing the handler plan:
  creating the worktree-local `.VE`, wiring the repo-root overlay, linking
  fallback root-bin tools, and writing the worktree-local `blackdog` launcher.
- Do not use or preserve deleted backlog/board/inbox/render flows or the old
  bootstrap/tune implementations unless they are explicitly rebuilt on top of
  the vNext core model.
- Repo lifecycle workflows such as install/update/refresh/tune are distinct
  from workset/task execution. If rebuilt, keep them in the product layer and
  do not encode them as workset/task semantics.
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
- Keep `blackdog` limited to product-layer WTAM orchestration plus repo
  lifecycle workflows on top of the typed core model.
- Keep `blackdog_cli` as a thin adapter over the shipped CLI surface. No
  domain logic belongs there.
- If a change needs client-specific context to make sense, it does not belong
  in core.

## Validation

- Run `make test` after meaningful Python changes.
- Run targeted CLI smoke checks when changing workset or WTAM behavior.
