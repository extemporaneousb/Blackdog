# Blackdog Emacs Workbench

Blackdog already has the right local data model for Emacs:

- `blackdog snapshot` is the read model for backlog state, objectives, runnable work, recent results, and artifact links.
- `blackdog supervise status --format json` and `blackdog supervise report --format json` are the operator-health views.
- the shared control dir is the artifact store for prompts, diffs, stdout/stderr, results, and inbox state.
- Git worktrees and task branches are the source of truth for code-state navigation.

This document defines the first Emacs 30+ package that sits on top of those surfaces.

## Design Goals

- keep Blackdog write semantics in the CLI and file contract instead of re-implementing state transitions in Elisp
- make backlog state, prompts, diffs, results, and worktrees browseable without leaving Emacs
- use Magit idioms where the operator already expects them
- keep optional packages optional and degrade cleanly to built-in completion/search where possible
- dogfood the package against this repo first, then grow the workflow surface by backlog task

## Frameworks To Leverage

### Core

- `magit-section`: use for the main backlog dashboard so the UI feels like a Magit status buffer, with expandable sections and stable motion semantics.
- `transient`: use for the top-level Blackdog dispatch menu instead of inventing a second command-dispatch UI.
- `tabulated-list-mode`: use for result and artifact indexes where sortable rows matter more than nested sections.
- built-in JSON/process APIs: use `json-parse-string`, `process-file`, and async process helpers rather than adding dependencies.

### Navigation and Search

- built-in completion: use `completing-read` as the baseline for task selection so Vertico, Icomplete, or default completion all work.
- `consult`: optional for artifact grep and richer incremental search when installed.
- `embark`: optional to act on task or artifact candidates in the minibuffer.
- `project.el` and `xref`: leverage existing project navigation for repo files and future Blackdog-aware symbol jumps.

### Git and Topic-Inspired Patterns

- `magit`: use for worktree status, branch range diffs, and commit inspection.
- `forge`: not a dependency, but a useful model for how a Magit-adjacent package can expose queue-like local objects and drill into detail buffers.

## Architecture

### Read path

1. Resolve repo root from `blackdog.toml`.
2. Prefer `./.VE/bin/blackdog` for that worktree; fall back to `blackdog` on `PATH`.
3. Call `blackdog snapshot` and cache the parsed JSON per root.
4. Render dashboard, task reader, and result buffers from snapshot rows.
5. Resolve artifact hrefs against `snapshot.control_dir`.

### Write path

The package should shell out to Blackdog CLI commands for any durable state change:

- claim/release/complete
- inbox messages
- result recording
- supervisor inspection/control

The first foundation slice keeps write commands minimal and focuses on browseability. Later tasks add safe write helpers and spec-driven task capture.

### Git / Worktree semantics

For a task row:

1. read `task_branch` and `target_branch` from the snapshot
2. use `git worktree list --porcelain` to map branch -> worktree path
3. open `magit-status` in that worktree when it still exists
4. open `magit-diff-range target..task` when the branch is still present
5. fall back to the saved `changes.diff` artifact when the branch/worktree has already been cleaned up

That gives the right behavior for both active WTAM work and landed historical tasks.

## Package Layout

The initial package layout is:

- `editors/emacs/lisp/blackdog-core.el`
- `editors/emacs/lisp/blackdog.el`
- `editors/emacs/lisp/blackdog-dashboard.el`
- `editors/emacs/lisp/blackdog-task.el`
- `editors/emacs/lisp/blackdog-results.el`
- `editors/emacs/lisp/blackdog-magit.el`
- `editors/emacs/test/blackdog-test.el`

Later backlog tasks will extend this with:

- artifact/thread buffers
- spec buffers and templates
- telemetry buffers
- richer search/navigation helpers

## UI Mock

```text
*Blackdog: Blackdog*

Blackdog
/Users/bullard/Work/Blackdog

Branch         agent/black-... -> main
Commit         43ea53b0e8b7
Latest Run     BLACK-... · Prepared · codex
Completed Time 14h 38m
Average Task   9m 53s

Overview
  Ready: 2  Claimed: 1  Waiting: 10  Done: 90
  Running: 1  Blocked: 0  Completed Today: 0  Total: 90

Objectives
  [0/2] Emacs workbench foundation
  [0/4] Interactive backlog cockpit

Board Tasks
  [Ready] BLACK-25d851d1c6  Define the Emacs workbench contract and scaffold the package core
    Lane: Emacs foundation  Wave: 0  Priority: P1
    Write the architecture/spec document, create the editors/emacs package skeleton...

Recent Results
  [success] BLACK-e429db183d  Added a machine-local tracked install registry...
```

The task reader is a separate buffer with:

- status, lane, wave, branch, target
- safe first slice and why
- latest result preview, what changed, validation, residual
- clickable project paths
- clickable prompt/stdout/stderr/diff/result links
- Magit status and diff commands

## Keybindings

Suggested prefix: `C-c b`

- `C-c b b`: open dashboard
- `C-c b r`: open results
- `C-c b t`: jump to a task by completion
- `C-c b a`: grep/search artifacts
- `C-c b m`: open Magit status for a task
- `C-c b d`: open branch diff for a task
- `C-c b g`: refresh the current Blackdog buffer

Inside the dashboard:

- `RET`: open the task reader or toggle a section
- `g`: refresh
- `r`: jump to results
- `s`: jump to a task
- `q`: quit window

Inside the task reader:

- `RET`: open the button at point
- `g`: refresh
- `m`: Magit status
- `d`: Magit diff

## Installation With use-package

Install the Emacs-side dependencies you want from ELPA/MELPA:

- `transient`
- `magit`
- `consult` (optional)
- `embark` (optional)

Then load Blackdog from this checkout:

```elisp
(use-package blackdog
  :load-path "/Users/bullard/Work/Blackdog/editors/emacs/lisp"
  :bind (("C-c b" . blackdog-dispatch)))
```

If you prefer a variable:

```elisp
(let ((blackdog-root "/Users/bullard/Work/Blackdog"))
  (use-package blackdog
    :load-path (list (expand-file-name "editors/emacs/lisp" blackdog-root))
    :bind (("C-c b" . blackdog-dispatch))))
```

## Testing Plan

### Foundation

- ERT unit tests for repo-root resolution, link resolution, task completion candidates, and worktree porcelain parsing
- one live snapshot smoke test against this repo to prove CLI/JSON wiring

### Feature wave

- fixture-driven ERT tests for dashboard rows, task reader rendering, and result buffers
- live smoke tests that open snapshot-backed buffers against this repo

### Git integration

- parser tests for `git worktree list --porcelain`
- manual smoke tests for active task worktrees, landed tasks, and fallback-to-saved-diff behavior

### Dogfood and telemetry

- run the package against this repo while Blackdog supervisor is active
- record refresh latency, missing-artifact failures, and workflow friction as follow-up backlog tasks

## Implementation Notes

- The first code slice keeps writes in the CLI and makes Emacs a high-signal operator cockpit.
- The spec-driven workflow belongs in a later lane after the dashboard, results, Magit, and search surfaces have stabilized.
- The package should stay dependency-light: optional packages improve UX, but the package must remain usable with built-in completion plus Magit/Transient.
