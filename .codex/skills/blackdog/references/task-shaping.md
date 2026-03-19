# Task Shaping

Use this reference when turning a user prompt into backlog tasks or when restructuring an existing plan.

## Objective

- Minimize the number of separate agent requests, worktrees, and handoffs while still minimizing end-to-end turnaround time.
- Prefer fewer, larger tasks that own a coherent deliverable and can land with one validation story.

## Measure First

Estimate these values before deciding whether to split work:

- `estimated_elapsed_minutes`: total wall-clock time to land and validate the deliverable.
- `estimated_active_minutes`: expected edit and reasoning time.
- `estimated_touched_paths`: approximate number of files or directories likely to change.
- `estimated_validation_minutes`: time to run the required checks.
- `estimated_worktrees`: how many branch-backed worktrees or child launches the plan would require.
- `estimated_handoffs`: how many times context would move between agents or tasks.
- `parallelizable_groups`: how many truly independent write scopes could run at the same time.

If the CLI has nowhere to store these fields, keep them in working notes and only record the meaningful constraints in task `why`, `evidence`, or comments.

## Consolidate by Default

Use one task when most of the following are true:

- the work shares the same touched paths or validation commands;
- one step depends directly on the previous step;
- the deliverable only makes sense as one landed change;
- worktree spin-up cost is material relative to the expected edit time; or
- handoff cost is likely to exceed any parallel speedup.

Use one lane for one cohesive deliverable. Do not create separate lane or task pairs for research, implementation, cleanup, and validation when they are parts of the same serial change.

## Split Only for Parallelism or Blocking

Split a task only when every child slice has:

- a disjoint or lightly coupled write set;
- an independently meaningful landed outcome;
- validation that can run mostly independently; and
- a believable wall-clock win from parallel execution.

Simple rule: split only when `parallel time saved > extra worktree spin-up + extra coordination + duplicated validation`.

## Tuning Loop

After completion, compare the estimates with actuals:

- actual elapsed time;
- actual changed path count;
- actual validation time;
- number of worktrees or agents used; and
- number of times work had to be merged, re-claimed, or re-scoped.

If consolidation repeatedly produces tasks that are too large, split later at the boundary that appeared in the real work. If parallel slices repeatedly collide in the same files or validations, merge them earlier next time.
