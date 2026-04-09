# Module Ownership Inventory

This document tags the current Blackdog surfaces by primary ownership so the next refactor can move code along clear boundaries instead of splitting by filename alone.

## Tags

- `blackdog_core`: durable runtime contract that other surfaces should depend on
- `blackdog`: Blackdog-owned product logic that should sit above `blackdog_core` and below adapters
- `adapter`: CLI, editor, scaffold, or packaging surface that should stay thin over `blackdog_core`/`blackdog` code
- `removal target`: legacy or compatibility surface that should be retired after the replacement path is complete

These tags describe primary ownership, not every dependency. A file may still need extraction even when it stays in the same tag.

## `src/blackdog_core`

| Path | Tag | Extraction target | Why |
| --- | --- | --- | --- |
| `src/blackdog_core/backlog.py` | `blackdog_core` | Keep as backlog/task/plan contract logic | It owns durable backlog parsing, task semantics, and plan interpretation. |
| `src/blackdog_core/profile.py` | `blackdog_core` | Keep as the config/profile boundary | It defines repo contract, path resolution, defaults, and profile loading. |
| `src/blackdog_core/state.py` | `blackdog_core` | Keep as the persistent runtime-state and record layer | It owns state, inbox, events, and results file I/O. |
| `src/blackdog_core/snapshot.py` | `blackdog_core` | Keep as the read-only snapshot entrypoint | It exposes runtime-artifact loading plus runtime and plan snapshot builders. |

## `src/blackdog`

| Path | Tag | Extraction target | Why |
| --- | --- | --- | --- |
| `src/blackdog/worktree.py` | `blackdog` | Keep together as WTAM lifecycle logic, separate from backlog/store code | It is Blackdog-specific orchestration around task branches, landing, and dirty-primary recovery. |
| `src/blackdog/supervisor.py` | `blackdog` | Keep together as supervisor runtime and child protocol logic | It owns launching, draining, recovery, run artifacts, and the delegated-child contract. |
| `src/blackdog/board.py` | `blackdog` | Keep as board/snapshot presentation code | It renders the static board and Blackdog-product projection model. |
| `src/blackdog/ui.css` | `blackdog` | Keep adjacent to the board renderer | It is a packaged asset for the static board surface, not a runtime primitive. |
| `src/blackdog/scaffold.py` | `blackdog` | Keep as bootstrap, refresh, and host-install workflow | It owns shipped project scaffolding and branded artifact generation on top of the core runtime contract. |
| `src/blackdog/conversations.py` | `blackdog` | Keep as the Blackdog-owned conversation artifact layer | It owns saved conversation threads and task linkage above the core write path. |
| `src/blackdog/installs.py` | `blackdog` | Keep as machine-local tracked-install registry handling | It is product-level host-management state, not core runtime contract. |
| `src/blackdog/__init__.py` | `adapter` | Keep minimal package metadata/export surface | It is packaging glue, not an ownership boundary. |

## `src/blackdog_cli`

| Path | Tag | Extraction target | Why |
| --- | --- | --- | --- |
| `src/blackdog_cli/main.py` | `adapter` | Keep as a thin command router over `blackdog_core` and `blackdog` | It is the main shell transport and should stay argument- and dispatch-focused. |
| `src/blackdog_cli/__main__.py` | `adapter` | Keep as the package entrypoint only | It is the `python -m blackdog_cli` shim. |

## `extensions/emacs`

The Emacs package is an adapter tree as a whole. The main split here is between kept adapter modules and legacy flows that should disappear after the Codex-first workflow fully lands.

| Path | Tag | Extraction target | Why |
| --- | --- | --- | --- |
| `extensions/emacs/lisp/blackdog-core.el` | `adapter` | Keep as shared Emacs process, JSON, cache, and path helpers | It is the package-local shell and snapshot bridge into Blackdog CLI. |
| `extensions/emacs/lisp/blackdog.el` | `adapter` | Keep as the top-level entrypoint and dispatch facade | It composes the other Emacs modules and user entry commands. |
| `extensions/emacs/lisp/blackdog-dashboard.el` | `adapter` | Keep as dashboard UI over snapshot data | It is a read-only operator view, not a source of durable state logic. |
| `extensions/emacs/lisp/blackdog-task.el` | `adapter` | Keep as the task reader and task-action buffer | It wraps CLI actions and artifact navigation around snapshot rows. |
| `extensions/emacs/lisp/blackdog-artifacts.el` | `adapter` | Keep as shared artifact-navigation helpers | It translates snapshot/task links into editor navigation. |
| `extensions/emacs/lisp/blackdog-results.el` | `adapter` | Keep as result-list UI | It is a tabulated read surface over snapshot data. |
| `extensions/emacs/lisp/blackdog-runs.el` | `adapter` | Keep as run-artifact UI | It is another read surface over snapshot/run artifacts. |
| `extensions/emacs/lisp/blackdog-search.el` | `adapter` | Keep as project/artifact search glue | It bridges Emacs completion/search packages to repo and control-dir data. |
| `extensions/emacs/lisp/blackdog-magit.el` | `adapter` | Keep as Magit integration | It is editor-specific navigation over WTAM state. |
| `extensions/emacs/lisp/blackdog-telemetry.el` | `adapter` | Keep as supervisor-monitor UI | It is an operator cockpit layered on CLI read/write commands. |
| `extensions/emacs/lisp/blackdog-codex.el` | `adapter` | Keep as the Codex-session adapter, possibly split into an optional package later | It is intentionally outside core runtime logic and talks to Codex session storage/CLI. |
| `extensions/emacs/lisp/blackdog-thread.el` | `removal target` | Retire after legacy Blackdog-owned conversation threads stop being needed | `docs/EMACS.md` already calls these legacy buffers kept for older prompt/task flows. |
| `extensions/emacs/lisp/blackdog-spec.el` | `removal target` | Retire after the Codex-first drafting path fully replaces spec-first authoring | `docs/EMACS.md` explicitly says spec-first is still available but no longer the default entrypoint. |
| `extensions/emacs/templates/blackdog-spec.md` | `removal target` | Remove with `blackdog-spec.el` | It only exists to support the legacy spec workflow. |
| `extensions/emacs/test/blackdog-test.el` | `adapter` | Keep with the Emacs adapter tree | It validates the editor integration, not the core runtime boundary. |
| `extensions/emacs/README.md` | `adapter` | Keep aligned with the Emacs adapter tree | It documents the optional editor surface. |

## `docs`

| Path | Tag | Extraction target | Why |
| --- | --- | --- | --- |
| `docs/ARCHITECTURE.md` | `core` | Keep as the runtime-boundary document | It defines the stable system split and WTAM/supervisor model. |
| `docs/FILE_FORMATS.md` | `core` | Keep as the storage/runtime contract | It is the canonical schema reference for backlog and runtime artifacts. |
| `docs/CLI.md` | `adapter` | Keep as the CLI transport reference | It documents shell-facing command surfaces rather than the underlying module boundaries. |
| `docs/INTEGRATION.md` | `adapter` | Keep as host-repo adoption guidance | It describes how outside repos consume Blackdog. |
| `docs/EMACS.md` | `adapter` | Keep as editor-specific guidance | It documents the optional Emacs surface, including its legacy paths. |
| `docs/CHARTER.md` | `blackdog` | Keep as product-direction guidance | It describes Blackdog's intended product shape rather than the durable runtime contract. |
| `docs/INDEX.md` | `adapter` | Keep as discovery/navigation only | It is the entrypoint into the doc set, not a source of runtime rules. |

## Recommended Extraction Order

1. Split `src/blackdog_core/backlog.py` first.
   The file mixes core task/state logic with planning summaries, prompt generation, and tuning heuristics.
2. Hold `src/blackdog/worktree.py`, `src/blackdog/supervisor.py`, and `src/blackdog/board.py` as separate `blackdog` seams.
   Those are distinct product subsystems and should stop depending on one oversized `backlog.py` module.
3. Keep `src/blackdog_cli/main.py`, `src/blackdog/scaffold.py`, and `extensions/emacs/**` thin.
   They should depend on extracted `blackdog_core`/`blackdog` modules instead of owning logic.
4. Remove compatibility and legacy surfaces last.
   `extensions/emacs/lisp/blackdog-thread.el`, `extensions/emacs/lisp/blackdog-spec.el`, and `extensions/emacs/templates/blackdog-spec.md` should stay only until the newer entrypoints cover their remaining use cases.
