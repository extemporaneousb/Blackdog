# Architecture

There are two supported operating modes for Blackdog:

- Direct manual mode for operator-driven development from the primary worktree.
- Delegated child mode for supervisor-launched branch-backed child runs.

Blackdog is a local-first backlog runtime for AI-assisted software work, with a target direction toward local multi-agent supervision.

Until the runtime-hardening tasks land, Blackdog's own repo should use
the direct WTAM path as its default operating mode: claim work
explicitly, run `blackdog worktree preflight|start`, make the change
in the task worktree, record `blackdog result record`, and then
land/complete manually. `blackdog supervise ...` and the static HTML
surface remain available for delegated runs and inspection, but they
are not the required control path for Blackdog-on-Blackdog
development.

## Core idea

The backlog system should live in the repo that depends on it. Skills should explain how to use it, but they should not be the source of executable state logic.

The layer contract for the remodel is frozen in [docs/BOUNDARIES.md](docs/BOUNDARIES.md):

- `core` owns the durable backlog/runtime primitives
- `blackdog proper` owns the shipped Blackdog product surface on top of those primitives
- `extensions` own optional adapters such as editor integrations

Integrations should compose through the stable `core` artifacts and
the documented `blackdog proper` CLI/write surfaces. Current Python
module names are not the adapter contract.

Current file placement is transitional and does not override that charter.

## Current architecture

Today Blackdog implements the durable backlog runtime, the
coordination primitives, a WTAM branch-backed worktree lifecycle
for implementation tasks, a draining supervisor runner, and a static
objective-first HTML board that embeds its own snapshot data. Claims,
inbox messages, structured results, HTML rendering, a canonical
snapshot contract, per-task workspaces, and child-agent launches
exist. Richer write-enabled runtime steering still does not.

## Core charter

The refactor target is a smaller `blackdog.core` that owns only the
durable, dependency-light runtime contract shared by every client.

Core owns:

- repo-local profile loading, path resolution, and canonical
  file-format contracts for backlog, state, events, inbox, and
  structured task results
- backlog parsing, plan interpretation, runnable-task selection, and
  dependency checks
- deterministic state transitions for claims, release, completion,
  comments, approvals, events, and structured results
- WTAM safety/read-model primitives such as workspace-contract
  inspection, branch/path facts, and dirty-check invariants other
  layers consume

Core does not own:

- worktree start/land/cleanup orchestration, rebasing, stashing, or
  landing policy
- thread/inbox operator workflows or delegated child-launch protocol
- HTML rendering or snapshot presentation choices
- project skill scaffolding or host bootstrap/refresh/update flows
- prompt/tune/report heuristics
- editor-facing conversation UX or client-specific thread workflows

Everything outside that boundary belongs in higher layers that depend
on core rather than extending it.

## Target package map

This is the target ownership map for extraction work. The layer names
describe the contract; exact file names can change as code moves.

1. `blackdog.core`
   - Durable backlog/runtime primitives.
   - Current homes: `config.py`, the durable backlog/task logic in
     `backlog.py`, state/result/thread storage in `store.py`, and the
     stable WTAM contract facts in `worktree.py`.

2. `blackdog.proper`
   - The shipped Blackdog product layered on top of core.
   - Owns the CLI surface, prompt/tune/report helpers, repo
     bootstrap/refresh/update behavior, project-local skill
     generation, the shipped static HTML board, and supervisor
     orchestration.
   - Current homes: `cli.py`, `skill_cli.py`, `supervisor.py`,
     `scaffold.py`, and the shipped render surface in `ui.py` and
     `ui.css`.

3. `extensions`
   - Optional adapters and operator-specific surfaces that consume
     Blackdog through documented contracts.
   - Owns editor integrations, alternate viewers, host-specific
     wrappers, and future environment-specific plugins.
   - Current homes: `editors/emacs/` and any future repo- or
     environment-specific integration package.

The adapter rule for this remodel is simple: integrations should read
stable artifact contracts from `core` and drive writes through
documented `blackdog proper` commands rather than private imports or
raw file edits.

## Non-goals for this slice

- Do not split Blackdog into multiple Python distributions. The target
  remains one `blackdog` package namespace with internal subpackages.
- Do not move durable write-path logic into optional extensions or
  client adapters.
- Do not let core depend on HTML/CSS assets, Codex launch details,
  prompt text, or editor UX.
- Do not treat current monolithic module names as a stable contract.
  The boundary matters; the interim filenames do not.
- Do not expand optional viewers into a write-enabled control plane.
  The shipped HTML board stays a readonly projection over snapshot
  data.

## Main layers

1. `blackdog.toml`
   - Repo-local profile.
   - Defines id prefix, bucket/domain taxonomy, defaults, and artifact paths.

2. `src/blackdog/`
   - Current implementation package for both `core` and `blackdog proper`.
   - Today it mixes backlog/runtime primitives with product surfaces
     such as CLI, scaffolding, HTML rendering, prompt helpers, and
     supervisor orchestration.
   - The remodel should separate those concerns according to
     `docs/BOUNDARIES.md` instead of treating the whole package as
     `core`.
	 

3. Shared git control root
   - Mutable artifact set resolved from `blackdog.toml`.
   - By default, `paths.control_dir = "@git-common/blackdog"` resolves
     to `<git-common-dir>/blackdog`, so every worktree in the repo
     sees the same `backlog.md`, `backlog-state.json`, `events.jsonl`,
     `inbox.jsonl`, `task-results/`, the repo-branded backlog HTML
     file, the compatibility `backlog-index.html` alias,
     `tracked-installs.json`, and
     `supervisor-runs/`.
	 
   - `blackdog.toml` stays repo-local, but runtime state is no longer
     a checked-in working-tree artifact.
	 

4. Project-local skill scaffold
   - Generated under `.codex/skills/<skill-name>/`.
   - Uses a project-specific wrapper token by default (`blackdog-<project-slug>`).
   - Tells an AI agent how to use the local CLI, local artifact paths, and the host repo's planning policy.

5. Extensions
   - Optional surfaces layered on top of documented Blackdog product
     contracts.
   - Today the clearest shipped example is the Emacs workbench under
     `editors/emacs/`, which should stay outside the minimal runtime
     charter.

## Ownership map

The current runtime still needs a finer file-level split than the high-level layers above. Use `docs/MODULE_INVENTORY.md` as the working inventory for:

- `core` runtime files that should define the durable contract
- `proper` Blackdog product subsystems that should sit above that core
- `extension` surfaces such as editor integrations and alternate viewers
- `removal target` compatibility or legacy surfaces that should disappear after newer paths fully replace them

## Why this split

- It avoids version skew between the repo and globally installed skill
  logic.
  
- It keeps stateful behavior testable.

- It preserves human-readable backlog markdown while moving execution
  state into structured files.
  
- It gives AI agents a durable message channel and structured
  task-result channel.
  

## Runtime model

1. `blackdog init` creates the profile and artifact set.
2. `blackdog add` appends backlog tasks and updates the plan block.
3. `blackdog claim`, `release`, `complete`, and `decide` update
   `backlog-state.json` and append `events.jsonl`.
4. `blackdog inbox ...` manages directed messages between user,
   supervisor, and child agents.
5. `blackdog result record` writes a task-result JSON file and appends
   an event.
6. `blackdog worktree preflight|start|land|cleanup` defines the
   current implementation-work lifecycle: start from the primary
   worktree branch, develop in a branch-backed task worktree, and land
   with fast-forward semantics.
7. `blackdog supervise run` starts with a cleanup sweep, compacts the
   active execution map, claims runnable tasks, allocates workspaces,
   launches child agents, rereads backlog and state while it is
   active, leaves newly completed tasks visible in place until the
   next run's opening sweep, and captures run artifacts until the run
   drains to idle or is stopped.
9. `blackdog snapshot` builds the canonical readonly monitor
   contract from backlog, state, inbox, events, results, and
   supervisor artifacts.
10. `blackdog render` rebuilds the static HTML control page from the
    current backlog, state, inbox, events, task results, and
    supervisor artifacts by embedding the snapshot JSON directly into
    the file.

This layout resolves mutable runtime files from one shared git control
root rather than repo-root runtime directories, so the working tree no
longer carries duplicate execution state.

## Strict core boundary audit

The current package already has a useful dependency shape: the UI,
scaffold, CLI, and supervisor layers all depend on `backlog.py`,
`store.py`, `worktree.py`, and `config.py`; those lower modules do not
import `ui.py`, `supervisor.py`, `scaffold.py`, or `cli.py` back. That
means the repo can tighten the core boundary by splitting files along
existing dependency direction rather than rewriting everything first.

For this repo, the strict ownership boundary should be:

- Strict core: durable backlog/state contracts, deterministic plan and
  selection logic, profile/path resolution, append-only artifact I/O,
  and the minimum git/workspace invariants required to keep WTAM safe.
- Blackdog proper: operator-facing orchestration built on that core,
  including CLI command composition, prompt/tune helpers, threads,
  inbox coordination, worktree lifecycle orchestration, repo
  bootstrap/refresh, generated skills, the shipped static board, and
  supervisor behavior.
- Extensions: optional editor integrations, alternate viewers, and
  host-specific wrappers that consume stable artifact and CLI
  contracts.

The current file-level ownership map is:

| File | Current role | Target owner |
| --- | --- | --- |
| `src/blackdog/config.py` | Repo profile and path resolution, including shared control-root layout. | Strict core. |
| `src/blackdog/backlog.py` | Backlog parser/validator/scheduler plus prompt/tune/view/render helpers. | Mixed. Split core parser/planner logic from Blackdog-proper prompt/view helpers. |
| `src/blackdog/store.py` | Atomic JSON/JSONL persistence plus inbox, thread, and result convenience APIs. | Mixed. Keep low-level artifact I/O and durable state/result persistence in core; move inbox/thread collaboration helpers to Blackdog proper. |
| `src/blackdog/worktree.py` | WTAM workspace contract, git worktree lifecycle, landing, rebasing, and cleanup. | Blackdog proper. Keep the safety contract, but do not treat branch orchestration as strict core. |
| `src/blackdog/cli.py` | User-facing command surface that composes every lower layer. | Blackdog proper. |
| `src/blackdog/supervisor.py` | Delegated child orchestration, launch prompts, recovery, and run artifacts. | Blackdog proper. |
| `src/blackdog/ui.py` | Static snapshot shaping and the shipped HTML rendering support. | Blackdog proper. |
| `src/blackdog/ui.css` | Styling for the shipped HTML board. | Blackdog proper. |
| `src/blackdog/scaffold.py` | Host-repo bootstrap, refresh, update, and project-local skill generation. | Blackdog proper. |
| `src/blackdog/skill_cli.py` | Skill-scaffold management entrypoint. | Blackdog proper. |
| `src/blackdog/__main__.py` | Thin executable wrapper around the CLI. | Blackdog proper. |
| `src/blackdog/__init__.py` | Package metadata surface. | Blackdog proper. |

The three target modules in this audit should be treated as follows:

| File | What must remain in strict core | What should move out of strict core |
| --- | --- | --- |
| `src/blackdog/backlog.py` | Backlog markdown/JSON parsing, task and plan validation, state sync, runnable-task selection, and task status classification. | Prompt profiles, tuning analysis, text rendering helpers, and other operator-facing narrative helpers. |
| `src/blackdog/store.py` | File locking, atomic writes, state/event loading, claim persistence, result persistence, and schema normalization for durable artifacts. | Inbox messaging, thread authoring/linkage, and other conversational collaboration helpers. |
| `src/blackdog/worktree.py` | The WTAM safety contract itself: primary-worktree detection, dirty-path checks, branch/path facts, and per-worktree `.VE` expectations. | Worktree creation, landing, cleanup, rebasing, stashing, and other orchestration commands that sit above the core artifact model. |

This implies a concrete refactor order:

1. Split `backlog.py` into a core backlog model/planner surface and a
   Blackdog-proper prompt/view surface.
2. Split `store.py` into core artifact persistence and Blackdog-proper
   inbox/thread collaboration services.
3. Treat `worktree.py` as Blackdog proper from the start; only extract
   tiny readonly WTAM facts into core if another package truly needs
   them.
4. Keep `ui.py`, `ui.css`, `scaffold.py`, and `skill_cli.py` in
   Blackdog proper while optional integrations continue to consume them
   through documented contracts.

For a Blackdog development checkout that manages multiple local host
repos, that same control root now also carries a machine-local tracked
install registry. It lets one development repo remember which local
Blackdog repos it should update and observe without checking that
developer-computer knowledge into any host repo.

The current supervisor launcher assumes an exec-capable Codex
runtime. With default settings, Blackdog prefers the desktop Codex.app
binary when it is installed and falls back to the configured launcher
command only if that runtime is unavailable.

For `git-worktree` launches, Blackdog creates a branch-backed child
worktree from the primary worktree branch and treats committed repo
state as the delegated baseline. If landing is later blocked by dirty
primary-worktree changes, Blackdog treats that as a contract
violation: it warns through the inbox and records the blocked child
outcome first, but it no longer leaves the repo in that state once the
run returns to an idle launch point. The supervisor now evaluates the
dirty primary checkout before any later child launch and either lands
the blocked branch after cleanup, commits a primary checkout that
already matches the blocked branch tree, or stashes unrelated dirty
state into an explicit follow-up backlog task so the queue can resume
without silently losing work.
Landing outcome is now surfaced in snapshots as:

- `latest_run_branch_ahead`: branch had committable changes relative to the target branch when the run ended.
- `latest_run_landed`: a landing commit was recorded for the run.
- `latest_run_land_error`: the landing failure reason when `latest_run_status` is blocked.

The supervisor-generated prompt tells the child that committed repo
state is the baseline, that the task is already claimed, that code
changes must be committed on the task branch, and that Blackdog CLI
output is the source of truth for coordination state.

For direct implementation work, Blackdog uses WTAM:

- create task worktrees from the primary worktree branch
- keep the task change isolated to that branch/worktree
- land with `--ff-only` semantics into the target branch
- clean up the task worktree after landing

That model is explicit in both `blackdog worktree ...` and
`blackdog supervise ...`. Delegated child runs use unique task
branches/worktrees and are landed through the primary worktree when
they exit cleanly with committable changes.

For Blackdog's own repo, that WTAM path is intentionally manual-first
until supervisor hardening lands. The supervisor and rendered control
surface are still useful, but operators should be able to continue
Blackdog development with the direct claim/worktree/result/complete
flow alone when runtime reliability is in doubt.

## Planning semantics

Blackdog's planning model separates executable work from concurrency
grouping:

- tasks are the only executable unit; claims, completion, results, and
  dependency checks all happen at task level
- lanes are temporary ordered slots in the execution map; lane order
  is preserved in the plan and UI, and the current scheduler advances
  lane tasks top-to-bottom
- waves group lanes that can open together for concurrent progress
  once every lower wave is finished, but they are reused and compacted
  between runs
- waves are scheduler gates, not dependency nodes; they describe when a
  set of lanes becomes eligible, while task-to-task predecessors still
  explain why one task is waiting on another

This distinction matters in the static control surface: the board
should lead with objective rows and summarize their completion state
without promoting lanes or waves into first-class UI objects. Lane
order and wave boundaries still matter inside the snapshot because they
drive progress rollups, next-focus selection, and scheduler semantics,
but the browser now presents those details through objective progress,
queue-health counts, and the task reader instead of a visible execution
map.

## Remodel evaluation and DAG reseeding

The current remodel spans core extraction, runtime hardening, Blackdog
dogfooding, and host-repo adapter surfaces. That breadth requires an
explicit evaluation loop so later tasks can change shape without losing
the target architecture.

### What checkpoints are allowed to change

Checkpoint review may change:

- task ordering
- lane and wave placement
- predecessor edges between tasks
- the size of the next implementation slice
- additional hardening or migration tasks needed to reach the same target

Checkpoint review must not change the target architecture implicitly. In
particular, a checkpoint does not by itself authorize moving stateful
behavior into prompt-only skills, abandoning WTAM for kept implementation
changes, downgrading the shared control-root contract, or changing how
repo-local runtime artifacts map to the documented file formats.

Those changes require an intentional update to the charter and
architecture docs first.

### Checkpoint criteria

The remodel should use four standing checkpoints:

1. Core extraction
   - Verify which logic belongs in the stable runtime/library boundary.
   - Verify which contracts must stay dependency-light and repo-local.
   - Verify that file-format and CLI documentation still describe the
     intended ownership boundaries.
2. Hardening
   - Verify that claims, inbox traffic, structured results, WTAM
     worktree flow, and delegated child startup/landing behavior are
     reliable enough to keep dogfooding.
   - Verify that failures are visible as product evidence and not
     hidden by operator-only recovery steps.
3. Blackdog proper
   - Verify that Blackdog's own repo still uses the documented
     manual-first contract when runtime-hardening work is incomplete.
   - Verify that backlog shaping, result reporting, and rendered status
     views are consistent with that contract.
4. Adapters
   - Verify that host-repo bootstrap, project-local skills, and related
     integration surfaces can adopt the remodel without hidden
     per-repo exceptions.

### Required checkpoint evidence

Each checkpoint should leave enough evidence that a later agent can
reseed the DAG from files instead of memory:

- doc updates when the intended contract changed
- durable discovery artifacts such as the target package map, module
  inventory, and other landed audit outputs that later tasks are meant
  to consume
- structured task results describing what changed, what was verified,
  and what remains open
- validation output appropriate to the touched surface
- a clear statement of whether the checkpoint preserved or revised the
  target architecture
- explicit follow-up backlog tasks for unresolved gaps

The evidence threshold is intentionally higher than "the current slice
works on my branch." A checkpoint is complete only when later work can
reconstruct the decision from docs, results, and backlog state.

### DAG reseeding protocol

When a checkpoint finishes, reseed the remaining DAG using existing
Blackdog planning objects only:

1. Compare the checkpoint outcome against `docs/CHARTER.md`, this
   architecture doc, `docs/MODULE_INVENTORY.md`, and the relevant
   completed task results that landed the checkpoint inputs.
2. If the target architecture still holds, add or reshape follow-up
   tasks without changing the target docs. Use task titles, `why`,
   `evidence`, `safe_first_slice`, and predecessor relationships to
   encode what the checkpoint learned.
3. If the checkpoint shows that the target architecture must change,
   update the target docs first, then reseed the remaining tasks
   against that revised target.
4. Preserve completed-task history. Reseeding should only modify
   unfinished work, task ordering, or gating relationships; claimed or
   completed tasks remain evidence, not rewrite targets.
5. Record the reseed rationale in structured results and, when useful,
   backlog comments so the next operator can see why the graph changed.

In practice, checkpoint-driven reseeding should bias toward adding the
smallest new task set that closes the newly discovered gap. Use extra
parallelism only when the revised predecessor graph and validation plan
still keep the charter legible.

## Static control surface

Blackdog's browser surface is now a rendered artifact, not a runtime
service:

- CLI commands write backlog/state/events/inbox/task-result artifacts
- supervisor runs write run artifacts and rerender the static HTML
- `blackdog render` rebuilds the repo-branded backlog HTML file (and
  refreshes the compatibility `backlog-index.html` alias) by embedding
  the current snapshot JSON directly in the file
- the page runs only local filtering and dialog behavior in the
  browser; it does not fetch, stream, or post state
- task cards link directly to on-disk artifacts such as result JSON,
  prompt/stdout/stderr logs, and captured child diffs

That keeps the communication path simple: file writers update the
control root, the renderer snapshots those files into one HTML view,
and the operator reloads the page when they want the latest state. The
page header also offers an optional auto-reload toggle with a fixed
countdown so operators can keep the static file cycling during active
runs without introducing a live data transport.
Each snapshot exposes both content freshness (`content_updated_at`) and
board freshness (`last_checked_at`) so operators can see how current the
visible file is. `content_updated_at` comes from the latest event timestamp
in the current snapshot source stream, while `last_checked_at` is derived from
the latest supervisor heartbeat (falling back to the snapshot generation time).
When the raw supervisor loop heartbeat matters, the snapshot also
includes `supervisor_last_checked_at`.

The rendered page now uses a wider control-board layout. It opens with
`Backlog Control` and `Status` in a split top band, follows with a
split objective/release-gates row, then ends with a split
`Execution Map` and `Completed Tasks` history. Objective and
release-gate rows are summary-only, and the objective table only shows
still-active objective rows while completed history keeps retired
objective context under each sweep. The live execution map and
completed-task cards open the task reader. The reader leads with
`What Changed` from the latest result payload, keeps `Summary`,
`Activity`, and run metadata immediately visible, and places variable
artifact/file details (`Paths`, `Checks`, `Docs`, `Validation`,
`Residual`) lower in the panel so the high-signal change narrative stays
top-most. Artifact navigation stays as plain text links, release gates
render as a checked table, and completed history keeps sweep
boundaries visible when run metadata exists.

The current supervisor run is inbox-steerable in a narrow way: open
`stop` messages addressed to the supervisor actor put the run into a
draining state, which prevents new launches while preserving repo-local
events and status files until already-running children finish. The
static HTML index is readonly: it surfaces tasks, results, and artifact
links, but intervention still flows back through chat and Blackdog CLI
writes.


## Delegated child startup and reporting

Blackdog's delegated child protocol is contract-first:

- The supervisor prompt already provides the task claim, branch, target,
  workspace mode, and workspace path.
- Child workspaces are considered valid if they point at branch-backed task
  trees and have the expected `.VE` handling for that checkout.
- Child agents should only run implementation code and `result record` in
  their delegated workspace.
- Landing, completion status, and recovery outcomes are captured in run metadata
  and surfaced through snapshots and supervision payloads.

Use `blackdog supervise report` to spot startup friction and output-shape
issues across recent runs before adjusting launch settings or launch instructions.

## WTAM audit

Against the WTAM baseline, Blackdog's current contract splits
into enforced surfaces, documented guidance, and open gaps.


| Requirement | Blackdog surfaces today | Audit |
| --- | --- | --- |
| No implementation from the primary worktree | `blackdog worktree preflight` reports whether the current checkout is primary, `blackdog worktree start` creates an external branch-backed task worktree, and supervisor child prompts always describe a branch-backed workspace. The CLI and config now hard-gate `git-worktree` as the only implementation mode. | Present. WTAM is the only kept-change implementation path. |
| Branch-backed task worktrees created from the primary branch | `blackdog worktree start|land|cleanup` plus supervisor workspace prep implement the lifecycle, keep worktrees outside the repo by default, and land through the primary worktree with fast-forward semantics. | Present. This is the strongest part of Blackdog's current WTAM surface. |
| Supervisor stays in the primary worktree; children commit on task branches and land through the primary worktree | Architecture, integration docs, tests, and generated child prompts all say the coordinator remains in the primary worktree while children work in task branches; prompts also forbid child-side landing or completion after a branch-backed run. | Present, but the child prompt is still the most explicit source for the commit/no-self-land rules. |
| Committed repo state is the baseline for delegated child work | Supervisor prompts tell children to treat committed repo state as the baseline, keep changes isolated to task scope, prefer Blackdog CLI output over raw state reads, and record structured results. | Partial. The delegated contract is explicit, but the repo-level docs and skill text had not mirrored it clearly enough. |
| Each git worktree carries its own `.VE` | Blackdog prefers `./.VE/bin/blackdog` when it exists, but the docs and generated skill did not previously say that `.VE/` is unversioned, absolute-path-bound, and per-worktree. | Gap. This is a documentation and scaffold-contract hole rather than a current CLI behavior. |

This audit tightens the repo-facing docs and skill text around the primary-worktree boundary, delegated-child contract, and per-worktree `.VE` rule. WTAM is now both the documented and enforced implementation path.

## Target architecture

The intended next layer is a repo-local supervisor runtime that can:

- turn backlog plan structure into active parallel work
- allocate worktrees or equivalent isolated workspaces to child agents
- capture child-agent status and structured results without hiding state outside the repo
- let a coordinating agent absorb user feedback, detect drift, and redirect future work through backlog and inbox updates

That target is described in [docs/CHARTER.md](docs/CHARTER.md). It is not fully implemented yet.
