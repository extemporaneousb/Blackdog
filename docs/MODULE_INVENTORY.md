# Module Ownership Inventory

This document tags the current Blackdog surfaces by primary ownership so the next refactor can move code along clear boundaries instead of splitting by filename alone.

## Tags

- `core`: durable runtime contract that other surfaces should depend on
- `proper`: Blackdog-owned product logic that should sit above core and below adapters
- `adapter`: CLI, editor, scaffold, or packaging surface that should stay thin over core/proper code
- `removal target`: legacy or compatibility surface that should be retired after the replacement path is complete

These tags describe primary ownership, not every dependency. A file may still need extraction even when it stays in the same tag.

## `src/blackdog`

| Path | Tag | Extraction target | Why |
| --- | --- | --- | --- |
| `src/blackdog/backlog.py` | `core` | Split into backlog model, planning, and prompt/tuning helpers under a core backlog package | It owns the task model and plan contract, but it is currently overloaded with prompt/tuning and summary logic. |
| `src/blackdog/config.py` | `core` | Keep as the config/profile boundary, possibly under `core/config` | It defines the repo contract, path resolution, defaults, and profile loading. |
| `src/blackdog/store.py` | `core` | Keep as the persistent state and locking layer, possibly under `core/store` | It owns state, inbox, events, results, threads, and tracked-install file I/O. |
| `src/blackdog/worktree.py` | `proper` | Keep together as WTAM lifecycle logic, separate from backlog/store code | It is Blackdog-specific orchestration around task branches, landing, and dirty-primary recovery. |
| `src/blackdog/supervisor.py` | `proper` | Keep together as supervisor runtime and child protocol logic | It owns launching, draining, recovery, run artifacts, and the delegated-child contract. |
| `src/blackdog/ui.py` | `proper` | Separate board/snapshot presentation code from core backlog/store code | It renders the static board and snapshot-facing presentation model. |
| `src/blackdog/ui.css` | `proper` | Keep adjacent to the board renderer | It is a packaged asset for the static board surface, not a runtime primitive. |
| `src/blackdog/proper/scaffold.py` | `proper` | Keep as Blackdog-product bootstrap, refresh, and host-install workflow | It owns shipped project scaffolding and branded artifact generation on top of the core runtime contract. |
| `src/blackdog/cli.py` | `adapter` | Keep as a thin command router over core/proper modules | It is the main shell transport and currently imports nearly every major subsystem. |
| `src/blackdog/__main__.py` | `adapter` | Keep as the package entrypoint only | It is only the `python -m blackdog` shim. |
| `src/blackdog/__init__.py` | `adapter` | Keep minimal package metadata/export surface | It is packaging glue, not an ownership boundary. |
| `src/blackdog/skill_cli.py` | `removal target` | Retire after `blackdog bootstrap` / `blackdog refresh` fully replace it | `docs/CLI.md` already treats `blackdog-skill` as a compatibility wrapper. |

## `editors/emacs`

The Emacs package is an adapter tree as a whole. The main split here is between kept adapter modules and legacy flows that should disappear after the Codex-first workflow fully lands.

| Path | Tag | Extraction target | Why |
| --- | --- | --- | --- |
| `editors/emacs/lisp/blackdog-core.el` | `adapter` | Keep as shared Emacs process, JSON, cache, and path helpers | It is the package-local shell and snapshot bridge into Blackdog CLI. |
| `editors/emacs/lisp/blackdog.el` | `adapter` | Keep as the top-level entrypoint and dispatch facade | It composes the other Emacs modules and user entry commands. |
| `editors/emacs/lisp/blackdog-dashboard.el` | `adapter` | Keep as dashboard UI over snapshot data | It is a read-only operator view, not a source of durable state logic. |
| `editors/emacs/lisp/blackdog-task.el` | `adapter` | Keep as the task reader and task-action buffer | It wraps CLI actions and artifact navigation around snapshot rows. |
| `editors/emacs/lisp/blackdog-artifacts.el` | `adapter` | Keep as shared artifact-navigation helpers | It translates snapshot/task links into editor navigation. |
| `editors/emacs/lisp/blackdog-results.el` | `adapter` | Keep as result-list UI | It is a tabulated read surface over snapshot data. |
| `editors/emacs/lisp/blackdog-runs.el` | `adapter` | Keep as run-artifact UI | It is another read surface over snapshot/run artifacts. |
| `editors/emacs/lisp/blackdog-search.el` | `adapter` | Keep as project/artifact search glue | It bridges Emacs completion/search packages to repo and control-dir data. |
| `editors/emacs/lisp/blackdog-magit.el` | `adapter` | Keep as Magit integration | It is editor-specific navigation over WTAM state. |
| `editors/emacs/lisp/blackdog-telemetry.el` | `adapter` | Keep as supervisor-monitor UI | It is an operator cockpit layered on CLI read/write commands. |
| `editors/emacs/lisp/blackdog-codex.el` | `adapter` | Keep as the Codex-session adapter, possibly split into an optional package later | It is intentionally outside core runtime logic and talks to Codex session storage/CLI. |
| `editors/emacs/lisp/blackdog-thread.el` | `removal target` | Retire after legacy Blackdog-owned conversation threads stop being needed | `docs/EMACS.md` already calls these legacy buffers kept for older prompt/task flows. |
| `editors/emacs/lisp/blackdog-spec.el` | `removal target` | Retire after the Codex-first drafting path fully replaces spec-first authoring | `docs/EMACS.md` explicitly says spec-first is still available but no longer the default entrypoint. |
| `editors/emacs/templates/blackdog-spec.md` | `removal target` | Remove with `blackdog-spec.el` | It only exists to support the legacy spec workflow. |
| `editors/emacs/test/blackdog-test.el` | `adapter` | Keep with the Emacs adapter tree | It validates the editor integration, not the core runtime boundary. |
| `editors/emacs/README.md` | `adapter` | Keep aligned with the Emacs adapter tree | It documents the optional editor surface. |

## `docs`

| Path | Tag | Extraction target | Why |
| --- | --- | --- | --- |
| `docs/ARCHITECTURE.md` | `core` | Keep as the runtime-boundary document | It defines the stable system split and WTAM/supervisor model. |
| `docs/FILE_FORMATS.md` | `core` | Keep as the storage/runtime contract | It is the canonical schema reference for backlog and runtime artifacts. |
| `docs/CLI.md` | `adapter` | Keep as the CLI transport reference | It documents shell-facing command surfaces rather than the underlying module boundaries. |
| `docs/INTEGRATION.md` | `adapter` | Keep as host-repo adoption guidance | It describes how outside repos consume Blackdog. |
| `docs/EMACS.md` | `adapter` | Keep as editor-specific guidance | It documents the optional Emacs surface, including its legacy paths. |
| `docs/CHARTER.md` | `proper` | Keep as product-direction guidance | It describes Blackdog's intended product shape rather than the durable runtime contract. |
| `docs/INDEX.md` | `adapter` | Keep as discovery/navigation only | It is the entrypoint into the doc set, not a source of runtime rules. |

## Recommended Extraction Order

1. Split `src/blackdog/backlog.py` first.
   The file mixes core task/state logic with planning summaries, prompt generation, and tuning heuristics.
2. Hold `src/blackdog/worktree.py`, `src/blackdog/supervisor.py`, and `src/blackdog/ui.py` as separate proper-layer seams.
   Those are distinct product subsystems and should stop depending on one oversized `backlog.py` module.
3. Keep `src/blackdog/cli.py`, `src/blackdog/proper/scaffold.py`, and `editors/emacs/**` thin.
   They should depend on extracted core/proper modules instead of owning logic.
4. Remove compatibility and legacy surfaces last.
   `src/blackdog/skill_cli.py`, `editors/emacs/lisp/blackdog-thread.el`, `editors/emacs/lisp/blackdog-spec.el`, and `editors/emacs/templates/blackdog-spec.md` should stay only until the newer entrypoints cover their remaining use cases.
