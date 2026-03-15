# Architecture

Blackdog is a local-first backlog runtime for AI-assisted software work, with a target direction toward local multi-agent supervision.

## Core idea

The backlog system should live in the repo that depends on it. Skills should explain how to use it, but they should not be the source of executable state logic.

## Current architecture

Today Blackdog implements the durable backlog runtime, the
coordination primitives, a WTAM-style branch-backed worktree lifecycle
for implementation tasks, an initial supervisor runner, an initial
persistent supervisor loop, and a served readonly live UI. Claims,
inbox messages, structured results, HTML rendering, a canonical UI
snapshot contract, per-task workspaces, and one-shot child-agent
launches exist. Richer active-run steering and drift-management
workflows still do not.


## Main layers

1. `blackdog.toml`
   - Repo-local profile.
   - Defines id prefix, bucket/domain taxonomy, defaults, and artifact paths.

2. `src/blackdog/`
   - Core runtime.
   - Owns backlog parsing, validation, selection, state transitions,
     events, inbox messages, structured results, HTML view generation,
     and the live UI snapshot/server.
	 

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
7. `blackdog supervise run` claims runnable tasks, allocates
   workspaces, launches child agents, and captures their run
   artifacts.
8. `blackdog supervise loop` repeats that cycle over time, records
   loop heartbeats, and refreshes the repo-local control surface.
9. `blackdog ui snapshot` builds the canonical readonly monitor
   contract from backlog, state, inbox, events, results, and
   supervisor artifacts.
10. `blackdog ui serve` serves that contract over local HTTP and
    pushes snapshot refreshes to the browser with SSE when Blackdog
    state changes.
11. `blackdog render` rebuilds the static HTML control page from the
    current backlog, state, inbox, events, and task results.

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
violation: it warns through the inbox, records a blocked result, and
leaves the child branch/worktree intact instead of mutating the
primary checkout with `git stash`.

The supervisor-generated prompt tells the child that committed repo
state is the baseline, that the task is already claimed, that code
changes must be committed on the task branch, and that Blackdog CLI
output is the source of truth for coordination state.

For direct implementation work, Blackdog now prefers a branch-backed
lifecycle that resembles WTAM:

- create task worktrees from the primary worktree branch
- keep the task change isolated to that branch/worktree
- land with `--ff-only` semantics into the target branch
- clean up the task worktree after landing

That model is explicit in both `blackdog worktree ...` and `blackdog supervise ...`. Delegated child runs now use unique task branches/worktrees and are landed through the primary worktree when they exit cleanly with committable changes.

The initial supervisor loop is inbox-steerable in a narrow way: open
`pause` messages addressed to the supervisor actor prevent new
launches, and `stop` messages terminate the loop while preserving
repo-local events and status files. The live UI is readonly: it
surfaces the graph, inbox, results, and active supervisor state, but
intervention still flows back through chat and Blackdog CLI writes.


## WTAM audit

Against the Utter WTAM baseline, Blackdog's current contract splits
into enforced surfaces, documented guidance, and open gaps.


| Requirement | Blackdog surfaces today | Audit |
| --- | --- | --- |
| No implementation from the primary worktree | `blackdog worktree preflight` reports whether the current checkout is primary, `blackdog worktree start` creates an external branch-backed task worktree, and supervisor child prompts always describe a branch-backed workspace. Public docs and skill text were weaker, and the CLI still exposes `supervise ... --workspace-mode current`. | Partial. Blackdog can surface the boundary, but it does not yet hard-reject every non-WTAM implementation path. |
| Branch-backed task worktrees created from the primary branch | `blackdog worktree start|land|cleanup` plus supervisor workspace prep implement the lifecycle, keep worktrees outside the repo by default, and land through the primary worktree with fast-forward semantics. | Present. This is the strongest part of Blackdog's current WTAM surface. |
| Supervisor stays in the primary worktree; children commit on task branches and land through the primary worktree | Architecture, integration docs, tests, and generated child prompts all say the coordinator remains in the primary worktree while children work in task branches; prompts also forbid child-side landing or completion after a branch-backed run. | Present, but the child prompt is still the most explicit source for the commit/no-self-land rules. |
| Committed repo state is the baseline for delegated child work | Supervisor prompts tell children to treat committed repo state as the baseline, keep changes isolated to task scope, prefer Blackdog CLI output over raw state reads, and record structured results. | Partial. The delegated contract is explicit, but the repo-level docs and skill text had not mirrored it clearly enough. |
| Each git worktree carries its own `.VE` | Blackdog prefers `./.VE/bin/blackdog` when it exists, but the docs and generated skill did not previously say that `.VE/` is unversioned, absolute-path-bound, and per-worktree. | Gap. This is a documentation and scaffold-contract hole rather than a current CLI behavior. |

This audit tightens the repo-facing docs and skill text around the primary-worktree boundary, delegated-child contract, and per-worktree `.VE` rule. A later enforcement slice should turn the remaining partial row into a repo-wide hard gate by removing or explicitly gating `workspace_mode = "current"` for WTAM-compliant repos.

## Target architecture

The intended next layer is a repo-local supervisor runtime that can:

- turn backlog plan structure into active parallel work
- allocate worktrees or equivalent isolated workspaces to child agents
- capture child-agent status and structured results without hiding state outside the repo
- let a coordinating agent absorb user feedback, detect drift, and redirect future work through backlog and inbox updates

That target is described in [docs/CHARTER.md](/Users/bullard/Work/Blackdog/docs/CHARTER.md). It is not fully implemented yet.
