# Target Model Execution Plan

The earlier compatibility-first migration plan is superseded.

This repo now treats the vNext sweep as the active plan:

1. make `planning.json` and `runtime.json` the durable source of truth
2. remove markdown planning compatibility logic
3. remove durable `epic`, `lane`, and `wave`
4. rebuild only the minimum coherent surfaces on top of the new core

The current shipped surface is:

- `blackdog repo install`
- `blackdog repo analyze`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog workset put`
- `blackdog task begin`
- `blackdog task show`
- `blackdog task land`
- `blackdog task close`
- `blackdog task cleanup`
- `blackdog worktree preflight`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog task cleanup`
- `blackdog summary`
- `blackdog next --workset`
- `blackdog snapshot`

Everything else should be considered removed, deferred, or subject to explicit
rebuild work rather than silent compatibility promises.

## Current Rewrite Prompt

Use this prompt to continue the rewrite:

> Blackdog is in an explicitly authorized breaking-change period. Delete or
> narrow old code that is not being carried forward. Do not preserve legacy
> backlog, compatibility shims, or old CLI behavior unless it is the fastest
> path to the new foundation.
>
> The base layer must be locked before breadth returns. Prioritize:
> 1. a rock-solid workset/task model
> 2. explicit workset/task claim semantics
> 3. the same-thread kept-change lifecycle:
>    `task begin -> execute -> task land`
> 4. durable accumulation of completed/landed work and attempt history
> 5. a clear execution model for direct work and any later delegated work
>
> Treat `task show`, `task close`, `task cleanup`, `worktree show`, and
> `worktree close` as the recovery and fallback surfaces around that
> canonical success path. Keep `worktree preflight|preview|start` as the
> explicit operator surfaces for planned workset/task execution.
>
> Treat WTAM as the normative kept-change workflow. Do not preserve a
> non-worktree execution mode in Blackdog.
>
> Prefer full removal of legacy product code over half-preserved compatibility.
> Keep `blackdog_cli` thin. Keep semantic/storage logic in typed core objects.
>
> Tests must use fresh isolated git repos. Favor low reasoning settings for
> bulk task-execution tests when that keeps the base layer cheap to validate.
>
> Locked decisions:
> - tasks exist inside a workset-owned DAG
> - claims attach to both worksets and tasks
> - the first-class execution models are `direct_wtam` and `workset_manager`
> - completed and landed history stays durable and operator-visible
