# Migration Guide

This guide explains how to move callers and operator habits onto the
post-remodel Blackdog surface without guessing from transitional module
placement.

Use it together with:

- [docs/CLI.md](docs/CLI.md) for command-level behavior
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md) for durable artifact
  schemas
- [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md) for the externally
  visible changes in this release line
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) for the final acceptance
  checklist and evidence sources

## Stable targets after the remodel

The remodel freezes these main migration targets:

- use the `blackdog` executable for command-line automation
- import `blackdog_core` for durable runtime contracts
- import `blackdog` for product workflows on top of that contract
- treat `blackdog_cli` as the parser/adapter package behind
  `blackdog`, not as a business-logic surface
- read shared machine facts from `blackdog snapshot` at
  `runtime_snapshot`
- treat top-level snapshot fields such as `tasks`, `board_tasks`,
  `queue_status`, run metadata, and artifact hrefs as UI projection
  fields around that machine contract
- use the documented WTAM lifecycle: `blackdog worktree preflight`,
  branch-backed task worktrees, and per-worktree `.VE/` environments

## CLI caller migration

For CLI automation and scripts:

- use `blackdog` for all commands
- map durable runtime commands mentally to `blackdog_core`
- map workflow/bootstrap/render/supervisor/thread/install commands
  mentally to `blackdog`

For module-level callers:

- do not depend on private or transitional module layout as a public
  API
- do not import `blackdog.scaffold`; the obsolete `blackdog.scaffold`
  shim was removed in the final cleanup pass
- prefer the documented CLI and artifact contracts over private Python
  imports when integrating another repo or tool

## Snapshot consumer migration

External readers should now treat `blackdog snapshot` as a product-owned
envelope with one stable shared contract nested inside it.

Move consumers to:

- `runtime_snapshot` for repo identity, counts, headers, plan rows,
  open inbox rows, next-runnable rows, and durable task state
- the top-level snapshot only for board/editor projections that still
  need task-reader affordances, artifact hrefs, or run metadata

Do not build new clients against duplicated top-level aliases when the
same data already exists in `runtime_snapshot`.

## Host repo migration

For repositories adopting Blackdog:

- use `blackdog create-project` for a brand-new repo
- use `blackdog bootstrap` for an existing repo
- use `blackdog refresh` after updating the installed Blackdog package
  or changing `blackdog.toml`
- commit `blackdog.toml` and the generated project-local skill if they
  are part of the repo contract
- do not check in mutable runtime artifacts from the shared control root
- keep `[taxonomy].doc_routing_defaults` aligned with the minimum docs
  agents must review before editing

WTAM remains the implementation contract for host repos too:

- start with `blackdog worktree preflight`
- do not make kept implementation edits in the primary worktree
- create or enter branch-backed task worktrees for implementation
- create a fresh repo-local `.VE/` in each worktree instead of copying
  one from another checkout

## Emacs operator migration

The Emacs package is already aligned to the remodeled contract:

- Codex sessions are the default chat surface
- shared backlog/runtime reads should come from `runtime_snapshot`
- task-reader and artifact-heavy views may still use the top-level
  projection fields the board emits

Legacy Emacs paths still remain, but they are no longer the default
workflow:

- `extensions/emacs/lisp/blackdog-thread.el`
- `extensions/emacs/lisp/blackdog-spec.el`
- `extensions/emacs/templates/blackdog-spec.md`

Use [docs/EMACS.md](docs/EMACS.md) for the full editor-specific
workflow details.

## Remaining compatibility surfaces

The final cleanup pass removed obsolete CLI/module shims. The main
remaining compatibility surfaces are duplicated top-level snapshot
aliases around `runtime_snapshot` plus the legacy Emacs thread/spec
helpers still listed in [docs/MODULE_INVENTORY.md](docs/MODULE_INVENTORY.md).
