# AGENTS

Blackdog is a repo-versioned backlog system built for AI-driven local development.

## Working Rules

- Keep the core dependency-light. Prefer the Python standard library
  unless a dependency is clearly justified.
- Use the current worktree's top-level `.VE` for Blackdog CLI
  invocations when it exists; prefer `./.VE/bin/blackdog` and
  `./.VE/bin/blackdog-skill` over a different `blackdog` on `PATH`.
- Treat kept implementation edits in the primary worktree as a
  contract violation. Before any repo edit you intend to keep, run
  `./.VE/bin/blackdog worktree preflight`; if it reports `primary
  worktree: yes`, stop and create or enter a branch-backed task
  worktree before touching repo files.
- Analysis-only work may stay in the current checkout, but
  implementation work should land from a task worktree created from
  the primary worktree branch.
- `.VE/` is not versioned. Each git worktree needs its own `.VE`
  rooted at that worktree; do not copy virtualenv directories between
  worktrees because they embed absolute paths.
- Until the runtime-hardening tasks land, run Blackdog's own repo in
  manual-first mode for operator work:
  - `blackdog claim` -> `blackdog worktree preflight|start` ->
    `blackdog result record` -> `land`/`complete` flow
  - `blackdog supervise ...` and static HTML are optional aids for
    inspection or delegated execution.
- For a delegated child workspace launched by supervisor, skip manual
  claim/preflight/bootstrap setup and follow the launch prompt:
  - the task is already claimed
  - committed repo state is the delegated baseline
  - use workspace-local `.VE` if present
  - commit changes on the child branch
  - report only through `blackdog result record`
- Blackdog uses WTAM for kept implementation changes. There is no
  non-WTAM implementation mode.
- Keep `[taxonomy].doc_routing_defaults` pointed at the docs agents
  must review before editing, and refresh the generated project skill
  when that routing changes so the repo contract stays explicit.
- Treat the file formats in `docs/FILE_FORMATS.md` as the contract for
  backlog, state, events, inbox, and task-result artifacts.
- Keep skills thin. If a change adds logic that belongs in the
  CLI/library, move it there instead of expanding prompt-only behavior.
- Preserve the self-hosted backlog in Blackdog's configured control
  root; use it to track Blackdog follow-up work.
- Update docs in `docs/` when CLI behavior or file formats change.

## Target Package Boundaries

- Keep `blackdog.core` limited to durable backlog/runtime contracts:
  profile/path resolution, canonical backlog/state/event/inbox/result
  formats, deterministic plan/state semantics, and WTAM safety facts
  other layers consume.
- `blackdog.core` explicitly excludes prompt/tune/report helpers,
  thread or inbox operator workflows, worktree start/land/cleanup
  orchestration, supervisor child-launch policy, bootstrap/refresh
  flows, and rendered HTML/view composition.
- Put prompt/tune policy, supervisor orchestration, task/thread
  operator flows, WTAM lifecycle orchestration, and
  bootstrap/refresh/update logic in `blackdog.proper`, not in core.
- Put readonly snapshot/view composition and static HTML/CSS
  rendering in `blackdog.viewers`; viewers must not become a write
  path.
- Keep shell/editor/Codex/skill entrypoints as thin
  `blackdog.adapters` over core/proper behavior.
- If a change needs client-specific context to make sense, it does not
  belong in core.

## Validation

- Run `make test` after meaningful Python changes.
- Run targeted CLI smoke checks when changing scaffold,
  add/claim/complete, inbox, result, or render behavior.
