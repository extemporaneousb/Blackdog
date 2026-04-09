# Remodel Release Notes

This release line closes the Blackdog remodel by publishing the final
operator contract, the migration path, and the remaining compatibility
story from one cleaned tree.

See [docs/MIGRATION.md](docs/MIGRATION.md) for step-by-step migration
guidance and [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) for the final
acceptance checklist.

## Highlights

- Froze the package split as `blackdog-core` / `blackdog_core`,
  `blackdog` / `blackdog`, and `blackdog-cli` / `blackdog_cli` while
  keeping the executable name `blackdog`.
- Froze `runtime_snapshot` as the shared machine contract for
  clients while preserving top-level snapshot aliases as compatibility
  projections for board-heavy readers.
- Locked the repo-local WTAM story around shared control-root state,
  branch-backed task worktrees, and per-worktree `.VE/` environments.
- Adapted the Emacs extension contract to the remodeled snapshot
  surface and Codex-first operator flow.
- Removed the obsolete `blackdog.scaffold` shim from the public tree.
- Removed the obsolete `blackdog_cli.skill` compatibility CLI.

## What changed for operators

Operators still use `blackdog` as the shell entrypoint. The difference
is architectural: `blackdog_cli` is now explicitly only the adapter,
while `blackdog_core` and `blackdog` own the actual behavior.

Host repos should prefer:

- `blackdog create-project` for brand-new repos
- `blackdog bootstrap` for existing repos
- `blackdog refresh` after package or profile changes

## What changed for integrations

External clients should now read machine-facing backlog/runtime data
from `blackdog snapshot` at `runtime_snapshot` instead of treating
board-shaped top-level fields as the long-term API.

Integrations should also treat the documented CLI and artifact
contracts, not the current Python module layout, as the public
integration surface.

## Compatibility notes

This release line intentionally keeps a small set of transitional
surfaces while downstream callers finish moving:

- top-level snapshot aliases remain around `runtime_snapshot`
- `extensions/emacs/lisp/blackdog-thread.el` remains for legacy
  Blackdog-owned prompt/task threads
- `extensions/emacs/lisp/blackdog-spec.el` and
  `extensions/emacs/templates/blackdog-spec.md` remain for the legacy
  spec-first Emacs drafting flow

Those remaining surfaces are documented explicitly so future removals
can happen in one deliberate patch instead of disappearing as hidden
drift.
