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

## Current architecture

Today Blackdog implements the durable backlog runtime, the
coordination primitives, a WTAM branch-backed worktree lifecycle
for implementation tasks, a draining supervisor runner, and a static
objective-first HTML board that embeds its own snapshot data. Claims,
inbox messages, structured results, HTML rendering, a canonical
snapshot contract, per-task workspaces, and child-agent launches
exist. Richer write-enabled runtime steering still does not.


## Main layers

1. `blackdog.toml`
   - Repo-local profile.
   - Defines id prefix, bucket/domain taxonomy, defaults, and artifact paths.

2. `src/blackdog/`
   - Core runtime.
   - Owns backlog parsing, validation, selection, state transitions,
     events, inbox messages, structured results, HTML view generation,
     and the static snapshot renderer.
	 

3. Shared git control root
   - Mutable artifact set resolved from `blackdog.toml`.
   - By default, `paths.control_dir = "@git-common/blackdog"` resolves
     to `<git-common-dir>/blackdog`, so every worktree in the repo
     sees the same `backlog.md`, `backlog-state.json`, `events.jsonl`,
     `inbox.jsonl`, `task-results/`, `backlog-index.html`, and
     `supervisor-runs/`.
	 
   - `blackdog.toml` stays repo-local, but runtime state is no longer
     a checked-in working-tree artifact.
	 

4. Project-local skill scaffold
   - Generated under `.codex/skills/blackdog/`.
   - Tells an AI agent how to use the local CLI and local artifact paths.

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

## Static control surface

Blackdog's browser surface is now a rendered artifact, not a runtime
service:

- CLI commands write backlog/state/events/inbox/task-result artifacts
- supervisor runs write run artifacts and rerender the static HTML
- `blackdog render` rebuilds `backlog-index.html` by embedding the
  current snapshot JSON directly in the file
- the page runs only local filtering and dialog behavior in the
  browser; it does not fetch, stream, or post state
- task cards link directly to on-disk artifacts such as result JSON,
  prompt/stdout/stderr logs, and captured child diffs

That keeps the communication path simple: file writers update the
control root, the renderer snapshots those files into one HTML view,
and the operator reloads the page when they want the latest state.

The rendered page now uses a wider control-board layout. It opens with
`Backlog Control` and `Status` in a split top band, follows with a
split objective/release-gates row, then ends with a split
`Execution Map` and `Completed Tasks` history. Objective and
release-gate rows are summary-only; the live execution map and
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

That target is described in [docs/CHARTER.md](/Users/bullard/Work/Blackdog/docs/CHARTER.md). It is not fully implemented yet.
