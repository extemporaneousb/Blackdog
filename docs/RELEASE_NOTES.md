# Remodel Release Notes

This release line closes the Blackdog remodel by publishing the final
operator contract, the migration path, and the remaining compatibility
story from one cleaned tree.

See [docs/MIGRATION.md](docs/MIGRATION.md) for step-by-step migration
guidance and [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) for the final
acceptance checklist.

## Highlights

- Split the public executables by ownership: `blackdog-core`,
  `blackdog-proper`, and `blackdog-devtool` now sit beside the
  compatibility `blackdog` umbrella CLI.
- Froze `snapshot.core_export` as the shared machine contract for
  clients while preserving top-level snapshot aliases as compatibility
  projections for board-heavy readers.
- Locked the repo-local WTAM story around shared control-root state,
  branch-backed task worktrees, and per-worktree `.VE/` environments.
- Adapted the Emacs extension contract to the remodeled snapshot
  surface and Codex-first operator flow.
- Removed the obsolete `blackdog.scaffold` shim from the public tree.

## What changed for operators

Operators can now route directly to the ownership-scoped executables
instead of treating `blackdog` as the only stable shell entrypoint.
`blackdog` still works, but it is now documented as the compatibility
umbrella over the mixed surface.

Host repos should prefer:

- `blackdog create-project` for brand-new repos
- `blackdog bootstrap` for existing repos
- `blackdog refresh` after package or profile changes

`blackdog-skill` still exists, but only as a compatibility wrapper
around the same bootstrap and refresh workflows.

## What changed for integrations

External clients should now read machine-facing backlog/runtime data
from `blackdog snapshot` at `core_export` instead of treating
board-shaped top-level fields as the long-term API.

Integrations should also treat the documented CLI and artifact
contracts, not the current Python module layout, as the public
integration surface.

## Compatibility notes

This release line intentionally keeps a small set of transitional
surfaces while downstream callers finish moving:

- `blackdog` remains the mixed compatibility CLI
- `blackdog-skill` remains a compatibility wrapper
- top-level snapshot aliases remain around `core_export`
- `editors/emacs/lisp/blackdog-thread.el` remains for legacy
  Blackdog-owned prompt/task threads
- `editors/emacs/lisp/blackdog-spec.el` and
  `editors/emacs/templates/blackdog-spec.md` remain for the legacy
  spec-first Emacs drafting flow

Those remaining surfaces are documented explicitly so future removals
can happen in one deliberate patch instead of disappearing as hidden
drift.
