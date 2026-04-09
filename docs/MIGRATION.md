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

- prefer `blackdog-core`, `blackdog-proper`, and `blackdog-devtool`
  when a caller wants one ownership-scoped surface
- keep `blackdog` only when a caller still needs the mixed
  compatibility umbrella CLI
- treat `blackdog-skill` as a compatibility wrapper, not the primary
  bootstrap or refresh entrypoint
- read shared machine facts from `blackdog snapshot` at
  `snapshot.core_export`
- treat top-level snapshot fields such as `tasks`, `board_tasks`,
  `queue_status`, run metadata, and artifact hrefs as UI projection
  fields around that machine contract
- use the documented WTAM lifecycle: `blackdog worktree preflight`,
  branch-backed task worktrees, and per-worktree `.VE/` environments

## CLI caller migration

For CLI automation and scripts:

- prefer `blackdog-core` for durable backlog/runtime commands
- prefer `blackdog-proper` for workflow, inbox, snapshot, render,
  supervisor, and thread flows
- prefer `blackdog-devtool` for `create-project`, `bootstrap`,
  `refresh`, `update-repo`, install management, and coverage
- keep `blackdog` in place only while a caller still depends on a mixed
  command surface
- replace direct `blackdog-skill` operator flows with
  `blackdog bootstrap` and `blackdog refresh`

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

- `snapshot.core_export` for repo identity, counts, headers, plan rows,
  open inbox rows, next-runnable rows, and durable task state
- the top-level snapshot only for board/editor projections that still
  need task-reader affordances, artifact hrefs, or run metadata

Do not build new clients against duplicated top-level aliases when the
same data already exists in `core_export`.

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
- shared backlog/runtime reads should come from `snapshot.core_export`
- task-reader and artifact-heavy views may still use the top-level
  projection fields the board emits

Legacy Emacs paths still remain, but they are no longer the default
workflow:

- `editors/emacs/lisp/blackdog-thread.el`
- `editors/emacs/lisp/blackdog-spec.el`
- `editors/emacs/templates/blackdog-spec.md`

Use [docs/EMACS.md](docs/EMACS.md) for the full editor-specific
workflow details.

## Intentional compatibility surfaces

The final cleanup pass removed obsolete shims but it did not retire
every compatibility layer at once. These surfaces still intentionally
remain:

- `blackdog` as the mixed compatibility umbrella CLI
- `blackdog-skill` as a compatibility wrapper around bootstrap and
  refresh flows
- duplicated top-level snapshot aliases around `snapshot.core_export`
- `editors/emacs/lisp/blackdog-thread.el`
- `editors/emacs/lisp/blackdog-spec.el`
- `editors/emacs/templates/blackdog-spec.md`

Those remaining paths are explicit compatibility or removal-target
surfaces, not hidden leftovers. See
[docs/MODULE_INVENTORY.md](docs/MODULE_INVENTORY.md) for their current
status.
