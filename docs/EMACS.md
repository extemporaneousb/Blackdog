# Blackdog Emacs Workbench

Blackdog already has the right local data model for Emacs:

- `blackdog snapshot` is the read model for backlog state, objectives, runnable work, recent results, and artifact links.
- `blackdog supervise status --format json` and `blackdog supervise report --format json` are the operator-health views.
- the shared control dir is the artifact store for prompts, diffs, stdout/stderr, results, and inbox state.
- Git worktrees and task branches are the source of truth for code-state navigation.

This document describes the shipped Emacs 30+ package that sits on top of those surfaces and how to run it as a local operator cockpit in this repo.

## Design Goals

- keep Blackdog write semantics in the CLI and file contract instead of re-implementing state transitions in Elisp
- make backlog state, prompts, diffs, results, and worktrees browseable without leaving Emacs
- use Magit idioms where the operator already expects them
- keep optional packages optional and degrade cleanly to built-in completion/search where possible
- dogfood the package against this repo first, then grow the workflow surface by backlog task

## Current Feature Surface

- dashboard buffer backed by `blackdog snapshot` with Magit-style sections for overview, objectives, board tasks, and recent results
- read-only task reader with task metadata, latest result summaries, clickable project paths, dedicated prompt/thread browsers, and clickable prompt/thread/stdout/stderr/diff/metadata/result/run artifacts
- tabulated listings for latest results and supervisor run directories
- shared root-local snapshot caching so one queue pass can reuse the same snapshot across dashboard, task, result, run, and artifact views until the operator explicitly refreshes
- Magit-aware task navigation that prefers live worktrees and live `target..task` diffs, then falls back to saved diff artifacts for historical tasks
- minibuffer completion for task, artifact, and project-file lookup
- incremental grep over the repo root or Blackdog control dir, with `consult-ripgrep` when available and `rgrep` otherwise
- spec-first authoring buffers that turn analysis notes into a `blackdog prompt` preview, a draft `blackdog add` payload, and direct create-or-launch actions
- task lifecycle commands for claim, claim-and-launch, release, complete, and remove without leaving Emacs
- telemetry buffer combining Emacs-side CLI timing/failure counters with `blackdog supervise status`, `recover`, and `report` summaries
- live supervisor monitor that can start one async `blackdog supervise run`, request a draining stop, open the latest run directory, and tail live child stdout/stderr artifacts from `supervisor-runs/`

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

## Dependency Tiers

| Tier | Packages | Why |
| --- | --- | --- |
| Required runtime | Emacs 30+, Blackdog CLI in the repo worktree | Core snapshot, task, artifact, result, run, search, spec, and telemetry commands shell out to `blackdog` and use built-in Emacs libraries. |
| Recommended | `magit` | Provides `magit-status`, `magit-diff-range`, and `magit-section`; the dashboard and Magit task actions are built around those surfaces. |
| Recommended | `transient` | Enables `blackdog-dispatch`; without it the prefix map still works, but `.` and `?` raise a clear error. |
| Optional | `consult` | Upgrades project and artifact grep to live `consult-ripgrep`. |
| Optional | `embark` | Adds minibuffer actions on task and artifact candidates without package-specific code. |

Notes:

- `magit-section` is currently pulled in through the `magit` package and is required for `blackdog-dashboard`.
- The package loads without Magit or Transient installed, but the commands that depend on them stay unavailable until those packages are present.

## Architecture

### Read path

1. Resolve repo root from `blackdog.toml`.
2. Prefer `./.VE/bin/blackdog` for that worktree; fall back to `blackdog` on `PATH`.
3. Call `blackdog snapshot` and cache the parsed JSON per root.
4. Reuse that cached snapshot across the current operator pass until the operator explicitly refreshes.
5. Render dashboard, task reader, and result buffers from snapshot rows.
6. Resolve artifact hrefs against `snapshot.control_dir`.

### Write path

The package should shell out to Blackdog CLI commands for any durable state change:

- claim/release/complete
- remove
- inbox messages
- result recording
- supervisor inspection/control

The current package keeps write logic thin and CLI-authoritative. Emacs adds safe wrappers for prompt shaping, task capture, and core task lifecycle actions, while Blackdog still owns the durable state transitions and validation rules.

### Git / Worktree semantics

For a task row:

1. read `task_branch` and `target_branch` from the snapshot
2. use `git worktree list --porcelain` to map branch -> worktree path
3. open `magit-status` in that worktree when it still exists
4. open `magit-diff-range target..task` when the branch is still present
5. fall back to the saved `changes.diff` artifact when the branch/worktree has already been cleaned up

That gives the right behavior for both active WTAM work and landed historical tasks.

### Prompt and thread browsing

For a task row:

1. `prompt_href` points at the saved child prompt when a supervisor run produced one.
2. `thread_href` points at the best available raw child transcript and currently prefers a non-empty `stderr.log`, then falls back to `stdout.log`.
3. direct/manual WTAM tasks may not have a thread artifact, so the reader leaves thread browsing unavailable instead of guessing.

The Emacs task reader uses dedicated prompt/thread buffers for those artifacts so operators can stay inside the workbench, while the raw files remain available through the artifact list and minibuffer artifact picker.

## Package Layout

The current package layout is:

- `editors/emacs/lisp/blackdog-core.el`
- `editors/emacs/lisp/blackdog.el`
- `editors/emacs/lisp/blackdog-dashboard.el`
- `editors/emacs/lisp/blackdog-task.el`
- `editors/emacs/lisp/blackdog-results.el`
- `editors/emacs/lisp/blackdog-runs.el`
- `editors/emacs/lisp/blackdog-search.el`
- `editors/emacs/lisp/blackdog-spec.el`
- `editors/emacs/lisp/blackdog-telemetry.el`
- `editors/emacs/lisp/blackdog-magit.el`
- `editors/emacs/templates/blackdog-spec.md`
- `editors/emacs/test/blackdog-test.el`

If you package this outside the repo checkout, keep `lisp/` and `templates/` together. The spec workflow resolves `blackdog-spec.md` relative to `blackdog-spec.el` unless you set `blackdog-spec-template-file`.

## Operator Workflows

### Daily queue pass

1. Open the dashboard with `C-c b b`.
2. Expand sections with Magit motion and hit `RET` on a task to open the task reader.
3. Use `m` for task-local Magit status or `d` for the task diff.
4. Use `r` and `u` from the prefix map to move into result and run listings.

### Task inspection and diff reading

For an active WTAM task:

1. `blackdog-magit-status-task` resolves the task branch to a live worktree with `git worktree list --porcelain`.
2. `blackdog-magit-diff-task` opens `target_branch..task_branch` in Magit when both branches still exist.

For a landed or cleaned-up task:

1. `blackdog-magit-status-task` falls back to the repo root when the task worktree no longer exists.
2. `blackdog-magit-diff-task` falls back to the saved `changes.diff` artifact when the task branch is gone.

That is the intended worktree-diff reading model: prefer live Git state while the task is active, then prefer the immutable saved diff once the task has been landed and cleaned up.

### Artifact and search pass

- `C-c b a` opens prompt/thread/stdout/stderr/diff/metadata/result/run artifacts through minibuffer completion.
- `C-c b A` searches the Blackdog control dir, which is the right place to grep old results, diffs, prompts, and supervisor artifacts.
- `C-c b f` and `C-c b s` stay in project code and docs.

### Spec-first task shaping

1. `C-c b n` opens a `Blackdog-Spec` buffer seeded from the bundled template.
2. Fill in the metadata and analysis sections.
3. Use `C-c C-p` to append code or data paths.
4. Use `C-c C-o` to preview the rewritten Blackdog prompt for that spec.
5. Use `C-c C-c` to render a draft payload buffer.
6. In the draft buffer, use `c` to create the task now or `w` to create it and immediately start its WTAM worktree.

This is intentionally spec-driven but still CLI-authoritative: Emacs helps shape and preview the request, then shells out to `blackdog prompt`, `blackdog add`, and `blackdog worktree start` for the durable workflow.

### Task lifecycle actions

From the task reader, task picker, or dispatch menu:

1. `c` or `C-c b c` claims a task for `blackdog-default-agent` unless you provide a prefix argument to enter a different actor.
2. `w` or `C-c b w` claims the task when needed and starts its WTAM worktree, then opens that worktree in Magit when available.
3. `l` or `C-c b l` releases a claimed task.
4. `e` or `C-c b e` completes a task with an optional note.
5. `k` or `C-c b k` removes a task after confirmation when the CLI allows removal.

Set `blackdog-default-agent` if you do not want Emacs writes to default to your login name.

### Telemetry and supervision

1. `C-c b v` opens the telemetry buffer.
2. `S` starts one asynchronous `blackdog supervise run` for the configured telemetry actor.
3. `x` sends a `stop` inbox control so the run drains after current child work.
4. `g` refreshes local counters plus `blackdog supervise status/recover/report`.
5. `u` opens the latest run directory and `o` jumps into the latest child artifact directory.
6. The monitor keeps polling live status while the run is active and tails the latest child `stderr.log` and `stdout.log` artifacts so the operator can watch agent output without leaving Emacs.

## UI Mocks

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

```text
*Blackdog Task: BLACK-62291a1166*

BLACK-62291a1166  Implement spec-driven buffers that link analyses, tasks, prompts, code, and data

Status         Completed
Objective      Emacs workbench
Lane           Authoring
Wave           2
Priority       P1
Risk           medium
Branch         agent/black-62291a1166-...
Target         main
Latest Result  success

Artifact Links
- Prompt
- Diff
- Result

Safe First Slice
Create the buffer and emit a draft task payload.

What Changed
- Added blackdog-spec.el and the bundled spec template.

Paths
- editors/emacs/lisp/blackdog-spec.el
- editors/emacs/templates/blackdog-spec.md
```

```text
*Blackdog Results*

Task           Status     Actor                Recorded                  Title
BLACK-781...   success    codex                2026-03-27T16:44:22Z      Expose supervisor telemetry...
BLACK-622...   success    codex                2026-03-27T16:35:05Z      Implement spec-driven buffers...
```

```text
*Blackdog Spec*

Title: Spec task
Bucket: integration
Priority: P1
Risk: medium
Effort: M
Objective: Spec-driven operator workflow

## Analysis
Capture a spec before creating the task.

## Code Paths
- editors/emacs/lisp/blackdog-spec.el
```

```text
*Blackdog Telemetry*

Session
Calls: 12  Failures: 1  Last error: blackdog supervise report ...

Supervisor Status
Actor: supervisor/emacs  Running: 0  Ready: 1  Waiting: 1

Supervisor Report
Startup healthy
Landing healthy
Retry pressure low
```

The spec workflow adds an editable `Blackdog-Spec` buffer that keeps:

- task metadata (`title`, `bucket`, `priority`, `risk`, `effort`, `objective`)
- analysis, why, evidence, and safe-first-slice notes
- separate code-path and data-path sections
- prompt notes that stay attached to the task draft without being forced into the backlog payload

`blackdog-spec-draft-task` renders that spec into:

- a draft `blackdog add` payload
- a ready-to-run `blackdog add` command
- prompt context that carries analysis, code paths, data paths, and prompt notes together

`blackdog-spec-prompt-preview` runs `blackdog prompt --format json` with a complexity calibrated from the spec effort, then renders the improved prompt in a read-only preview buffer before you create the task.

The telemetry workflow adds a read-only `Blackdog-Telemetry` buffer that combines:

- session-local Emacs instrumentation for Blackdog CLI latency and failures
- `blackdog supervise status --format json` summaries for the current supervisor actor
- `blackdog supervise recover --format json` summaries for recoverable blocked/interrupted runs
- `blackdog supervise report --format json` summaries for startup, retry, output-shape, and landing health
- live links into the latest child artifact directories plus tailed child `stderr.log` and `stdout.log` output

## Keybindings

Suggested prefix: `C-c b`

### Prefix map

| Key | Command | Purpose |
| --- | --- | --- |
| `C-c b b` | `blackdog-dashboard` | Open the Magit-style dashboard. |
| `C-c b c` | `blackdog-claim-task` | Claim a task from completion. |
| `C-c b w` | `blackdog-launch-task` | Claim and start a task worktree from completion. |
| `C-c b l` | `blackdog-release-task` | Release a claimed task from completion. |
| `C-c b e` | `blackdog-complete-task` | Complete a task from completion. |
| `C-c b k` | `blackdog-remove-task` | Remove a task from completion when the CLI allows it. |
| `C-c b x` | `blackdog-start-supervisor` | Start the telemetry actor's supervisor run and open the live monitor. |
| `C-c b X` | `blackdog-stop-supervisor` | Request a draining stop for the telemetry actor's supervisor run. |
| `C-c b u` | `blackdog-runs-open` | Browse supervisor run directories. |
| `C-c b r` | `blackdog-results-open` | Browse latest task results. |
| `C-c b t` | `blackdog-find-task` | Open a task reader from completion. |
| `C-c b a` | `blackdog-find-artifact` | Open a prompt/diff/result/run artifact from completion. |
| `C-c b n` | `blackdog-spec-new` | Start a new spec-first task draft. |
| `C-c b v` | `blackdog-telemetry-open` | Open CLI and supervisor telemetry. |
| `C-c b f` | `blackdog-find-project-file` | Jump to a repo file. |
| `C-c b s` | `blackdog-search-project` | Search the repo root. |
| `C-c b A` | `blackdog-search-artifacts` | Search the Blackdog control dir. |
| `C-c b m` | `blackdog-magit-status-for-task` | Open Magit status for a task. |
| `C-c b d` | `blackdog-magit-diff-for-task` | Open the task diff or saved diff artifact. |
| `C-c b .` | `blackdog-dispatch` | Open the Transient dispatch menu. |
| `C-c b ?` | `blackdog-dispatch` | Same as `.`. |
| `C-c b g` | `blackdog-refresh` | Clear the cached snapshot and refresh the current Blackdog buffer. |

### Dashboard keys

| Key | Purpose |
| --- | --- |
| `RET` | Open the task/result at point or toggle the current section. |
| `g` | Clear the cached snapshot and refresh the buffer. |
| `r` | Jump to results. |
| `s` | Jump to a task by completion. |
| `q` | Quit the window. |

### Task reader keys

| Key | Purpose |
| --- | --- |
| `RET` | Open the button at point. |
| `g` | Clear the cached snapshot and refresh the task reader. |
| `c` | Claim the current task. |
| `w` | Claim when needed and start the task worktree. |
| `l` | Release the current task. |
| `e` | Complete the current task. |
| `k` | Remove the current task after confirmation. |
| `m` | Open Magit status for the task. |
| `d` | Open a live Magit diff or saved diff artifact. |
| `p` | Browse the prompt in a read-only Blackdog buffer. |
| `t` | Browse the best available child thread transcript. |
| `P` | Open the raw prompt artifact. |
| `r` | Open the latest result artifact. |
| `O` | Open stdout. |
| `E` | Open stderr. |
| `D` | Open diff. |
| `M` | Open metadata. |
| `F` | Open result JSON. |
| `R` | Open the run artifact directory. |

### Results and runs

| Buffer | Keys |
| --- | --- |
| Results | `RET` opens the task reader, `f` opens the result file, `g` clears the cached snapshot and refreshes. |
| Runs | `RET` opens the run directory, `t` opens the task reader, `g` clears the cached snapshot and refreshes. |

### Spec and telemetry

| Buffer | Keys |
| --- | --- |
| Spec | `C-c C-o` previews the rewritten prompt, `C-c C-c` renders the draft payload, `C-c C-p` appends a code or data path. |
| Spec Draft | `p` refreshes the prompt preview, `c` creates the task, `w` creates and launches it, `g` rerenders the draft. |
| Telemetry | `S` starts one async supervisor run, `x` requests a draining stop, `u` opens the latest run directory, `o` opens the latest child artifact directory, `r` opens the run listing, `g` refreshes supervisor/session data, `c` clears the session counters and refreshes. |

## Installation With use-package

Install the Emacs-side dependencies you want from ELPA/MELPA first:

- `magit`
- `transient`
- `consult` (optional)
- `embark` (optional)

Then load Blackdog from this checkout:

```elisp
(use-package magit
  :ensure t)

(use-package transient
  :ensure t)

(use-package consult
  :ensure t
  :defer t)

(use-package embark
  :ensure t
  :defer t)

(use-package blackdog
  :load-path "/Users/bullard/Work/Blackdog/editors/emacs/lisp"
  :custom
  (blackdog-default-agent "bullard")
  :bind-keymap (("C-c b" . blackdog-prefix-map)))
```

If you prefer a variable:

```elisp
(let ((blackdog-root "/Users/bullard/Work/Blackdog"))
  (use-package blackdog
    :load-path (list (expand-file-name "editors/emacs/lisp" blackdog-root))
    :custom
    (blackdog-default-agent "bullard")
    :bind-keymap (("C-c b" . blackdog-prefix-map))))
```

For a vendored install outside this repo layout, either keep the `templates/` directory next to `lisp/` or set:

```elisp
(setq blackdog-spec-template-file
      "/path/to/blackdog/editors/emacs/templates/blackdog-spec.md")
```

After loading the package:

1. Open a file inside a repo containing `blackdog.toml`.
2. Run `C-c b b` to confirm the dashboard renders.
3. Run `C-c b .` to confirm Transient is available.
4. Run `C-c b m` on any task to confirm Magit integration is present.
5. Run `C-c b n`, preview a prompt with `C-c C-o`, then create and launch a task from the draft buffer with `w`.
6. Run `C-c b v`, start a supervisor run with `S`, verify live child output appears, then stop it with `x`.

## Release Packaging For Emacs 30+

The current release model is a local-use package loaded directly from a checkout or vendored subtree. There is no MELPA or GNU ELPA release contract yet.

For an internal release bundle, ship:

- `editors/emacs/lisp/*.el`
- `editors/emacs/templates/blackdog-spec.md`
- `editors/emacs/README.md`
- `docs/EMACS.md`

Recommended release checklist:

1. Install the Blackdog CLI in the target worktree-local `.VE`.
2. Install `magit` and `transient` in Emacs.
3. Load the package through `use-package` or an equivalent `load-path` setup.
4. Run the batch checks in the testing section below.
5. Open the dashboard against a real repo and verify one live task diff plus one landed-task saved diff.
6. Open `C-c b v`, start a supervisor run with `S`, confirm the monitor tails live child output, and stop it with `x`.

## Testing Plan

### Foundation

- ERT unit tests for repo-root resolution, link resolution, task completion candidates, and worktree porcelain parsing
- one live snapshot smoke test against this repo to prove CLI/JSON wiring

### Feature wave

- fixture-driven ERT tests for dashboard rows, task reader rendering, and result buffers
- live smoke tests that open snapshot-backed buffers against this repo
- direct ERT coverage for task/artifact completion, project-aware file navigation, and search root selection
- spec-buffer coverage for template loading, path capture, and draft task command generation
- telemetry coverage for CLI call instrumentation and supervisor summary rendering

### Git integration

- parser tests for `git worktree list --porcelain`
- manual smoke tests for active task worktrees, landed tasks, and fallback-to-saved-diff behavior

### Dogfood and telemetry

- run the package against this repo while Blackdog supervisor is active
- record refresh latency, missing-artifact failures, and workflow friction as follow-up backlog tasks

## Development And Validation

Run the package checks from the repo root:

```bash
make test-emacs
emacs -Q --batch -L editors/emacs/lisp -L editors/emacs/test \
  -l editors/emacs/test/blackdog-test.el \
  -f ert-run-tests-batch-and-exit
make test
```

Useful manual smoke checks:

- `emacs --batch --eval "(progn (package-initialize) (add-to-list 'load-path \".../editors/emacs/lisp\") (require 'blackdog))"`
- open `C-c b b`, `C-c b r`, `C-c b u`, and `C-c b v` in this repo
- open one task and verify `p` renders the prompt browser and `t` renders the thread browser when supervisor artifacts exist
- open `C-c b n`, preview a prompt with `C-c C-o`, then create and launch a task from the draft buffer with `w`
- open `C-c b v`, start the supervisor with `S`, confirm the live child stderr/stdout tails appear, then request stop with `x`
- verify `C-c b d` uses a live Magit diff for an active task and a saved `changes.diff` artifact for a landed task

## Implementation Notes

- The current code keeps durable writes in the CLI and makes Emacs a high-signal operator cockpit.
- The package should stay dependency-light: optional packages improve UX, but the package must remain usable with built-in completion plus Magit/Transient.
- Snapshot reloads are now explicit: opening multiple read-only workbench buffers during one queue pass reuses the cached snapshot, while `g` and `blackdog-refresh` clear that cache before reloading.
- The next backlog candidates are write-enabled inbox/approval flows, richer minibuffer actions, and asynchronous refresh for larger control dirs.
