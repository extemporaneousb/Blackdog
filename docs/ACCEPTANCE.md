# Final Acceptance Evidence

This document is the versioned closeout checklist for the remodel
release line. Acceptance means the repo, docs, tests, and remaining
compatibility seams all describe the same final surface.

## Acceptance gates

The remodel closeout is accepted when these statements are true in one
tree:

- public executables and packaging scope are frozen together in
  `pyproject.toml`, [README.md](../README.md),
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and
  [docs/CLI.md](docs/CLI.md)
- shared machine-facing snapshot readers use `runtime_snapshot`,
  while top-level snapshot aliases are treated as compatibility
  projections
- WTAM remains the only kept-change implementation path, with shared
  control-root state and branch-backed task worktrees
- host integration guidance routes callers through documented CLI flows
  and stable artifact files instead of hand-edited state or private
  imports
- the obsolete `blackdog.scaffold` shim is gone
- the remaining compatibility surfaces are explicitly named and
  intentionally tracked

## Evidence sources

Use these files as the acceptance record:

- `pyproject.toml`
- [README.md](../README.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/CLI.md](docs/CLI.md)
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md)
- [docs/INTEGRATION.md](docs/INTEGRATION.md)
- [docs/MIGRATION.md](docs/MIGRATION.md)
- [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md)
- [docs/MODULE_INVENTORY.md](docs/MODULE_INVENTORY.md)
- [extensions/emacs/README.md](../extensions/emacs/README.md)
- [tests/test_core_contracts.py](../tests/test_core_contracts.py)

Together these sources should show:

- the `blackdog` executable contract plus the
  `blackdog_core` / `blackdog` / `blackdog_cli` package split
- the `runtime_snapshot` snapshot contract
- the WTAM and shared-control-root operating model
- the host-repo bootstrap and refresh path
- the final removal of `blackdog.scaffold`
- the still-intentional compatibility surfaces:
  - top-level snapshot aliases
  - `extensions/emacs/lisp/blackdog-thread.el`
  - `extensions/emacs/lisp/blackdog-spec.el`
  - `extensions/emacs/templates/blackdog-spec.md`

## Validation commands

The repo-level acceptance pass is:

```bash
make acceptance
```

That alias intentionally keeps the closeout proof simple:

- `make test`
- `make test-emacs`

Use `make coverage-core` as the focused core audit artifact when the
change also needs the shipped-surface coverage report.

## Exit rule for future cleanup

If a future patch removes one of the remaining compatibility surfaces,
update this document, [docs/MIGRATION.md](docs/MIGRATION.md),
[docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md), and the corresponding
contract tests in the same change so the acceptance story stays
explicit.
