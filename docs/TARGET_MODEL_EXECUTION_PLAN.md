# Target Model Execution Plan

The earlier compatibility-first migration plan is superseded.

This repo now treats the vNext sweep as the active plan:

1. make `planning.json` and `runtime.json` the durable source of truth
2. remove markdown planning compatibility logic
3. remove durable `epic`, `lane`, and `wave`
4. rebuild only the minimum coherent surfaces on top of the new core

The current shipped slice is deliberately narrow:

- `blackdog workset put`
- `blackdog worktree preflight`
- `blackdog worktree start`
- `blackdog worktree land`
- `blackdog worktree cleanup`
- `blackdog summary`
- `blackdog next`
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
> 2. explicit claim semantics
> 3. the kept-change WTAM lifecycle:
>    `worktree preflight -> worktree start -> execute -> worktree land -> worktree cleanup`
> 4. durable accumulation of completed/landed work and attempt history
> 5. a clear execution model for direct work and any later delegated work
>
> Treat WTAM as the normative kept-change workflow. If analysis-only work is
> supported, it must be a separate command family and not a mode switch hidden
> inside the kept-change path.
>
> Prefer full removal of legacy product code over half-preserved compatibility.
> Keep `blackdog_cli` thin. Keep semantic/storage logic in typed core objects.
>
> Tests must use fresh isolated git repos. Favor low reasoning settings for
> bulk task-execution tests when that keeps the base layer cheap to validate.
>
> The main unresolved design questions to close next are:
> - how tasks exist inside worksets
> - what is claimed: tasks, worksets, or both
> - which execution models are first-class
> - what completion/landed history is durable and operator-visible
