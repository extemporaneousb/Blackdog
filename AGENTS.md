# AGENTS

Blackdog is a repo-versioned backlog system built for AI-driven local development.

## Working Rules

- Keep the core dependency-light. Prefer the Python standard library unless a dependency is clearly justified.
- Use the current worktree's top-level `.VE` for Blackdog CLI invocations when it exists; prefer `./.VE/bin/blackdog` and `./.VE/bin/blackdog-skill` over a different `blackdog` on `PATH`.
- If you will keep repo edits, treat Blackdog's WTAM flow as mandatory: run `./.VE/bin/blackdog worktree preflight` first and, when it reports `primary worktree: yes`, do not edit in that checkout; create or enter a branch-backed task worktree before touching repo files.
- Analysis-only work may stay in the current checkout, but implementation work should land from a task worktree created from the primary worktree branch.
- `.VE/` is not versioned. Each git worktree needs its own `.VE` rooted at that worktree; do not copy virtualenv directories between worktrees because they embed absolute paths.
- Keep `supervisor.workspace_mode = "git-worktree"` for repos that want a WTAM-style hard gate. Treat `current` as a compatibility escape hatch, not the default implementation contract.
- Treat the file formats in `docs/FILE_FORMATS.md` as the contract for backlog, state, events, inbox, and task-result artifacts.
- Keep skills thin. If a change adds logic that belongs in the CLI/library, move it there instead of expanding prompt-only behavior.
- Preserve the self-hosted backlog in Blackdog's configured control root; use it to track Blackdog follow-up work.
- Update docs in `docs/` when CLI behavior or file formats change.

## Validation

- Run `make test` after meaningful Python changes.
- Run targeted CLI smoke checks when changing scaffold, add/claim/complete, inbox, result, or render behavior.
