# Operator Notes

Operational notes record intentional cleanup decisions that should remain
discoverable after transient runtime references or branches are removed.

## 2026-07-08: Defunct `v3` Task References

Three abandoned task rows still pointed at target branch `v3`, which no longer
exists. Each branch had no live worktree, had zero commits ahead of its tip, and
was already an ancestor of current `main` (`git rev-list --left-right --count
main...<branch>` reported `31 0`; `git merge-base --is-ancestor <branch> main`
returned success). The branch tip for all three was
`2174a52816e5a47bd79feedc6552e294524e2ebf`.

These references are obsolete and should not be preserved:

- `task-add-an-explicit-task-recovery-surface-for-stale-claims-and-dirty-task-85de4eeb`
  / `TASK-1`
  / `agent/task-add-an-explicit-task-recovery-surface-for-stale-claims-and-dirty-task-85de4eeb-task-1-add-an-explicit-task-recovery-surface-for-stale-claims-and-dirty-task`
- `task-tighten-attempt-history-prompt-lineage-so-summary-table-surfaces-stop-81556799`
  / `TASK-1`
  / `agent/task-tighten-attempt-history-prompt-lineage-so-summary-table-surfaces-stop-81556799-task-1-tighten-attempt-history-prompt-lineage-so-summary-table-surfaces-stop`
- `task-revisit-landed-commit-format-for-richer-prompt-lineage-and-execution-799a0e56`
  / `TASK-1`
  / `agent/task-revisit-landed-commit-format-for-richer-prompt-lineage-and-execution-799a0e56-task-1-revisit-landed-commit-format-for-richer-prompt-lineage-and-execution`

The cleanup action is to cancel the task rows as superseded and delete these
local branch refs. If a future archaeological review needs the content, current
`main` already contains the tip commit.
