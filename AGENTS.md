# AGENTS

Blackdog is a repo-versioned backlog system built for AI-driven local development.

## Working Rules

- Keep the core dependency-light. Prefer the Python standard library unless a dependency is clearly justified.
- Treat the file formats in `docs/FILE_FORMATS.md` as the contract for backlog, state, events, inbox, and task-result artifacts.
- Keep skills thin. If a change adds logic that belongs in the CLI/library, move it there instead of expanding prompt-only behavior.
- Preserve the self-hosted backlog under `.blackdog/`; use it to track Blackdog follow-up work.
- Update docs in `docs/` when CLI behavior or file formats change.

## Validation

- Run `make test` after meaningful Python changes.
- Run targeted CLI smoke checks when changing scaffold, add/claim/complete, inbox, result, or render behavior.

