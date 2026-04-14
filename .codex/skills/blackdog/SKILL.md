---
name: blackdog
description: "Use the repo-local Blackdog CLI and contract for Blackdog."
---

# Repo Skill: Blackdog

Use the repo-local Blackdog CLI instead of mutating control-root files by hand.
`blackdog.toml` is the machine-readable source of truth for handler setup and routed docs.
Keep hard repo rules in `AGENTS.md` and the routed docs below; this skill is the generated Blackdog summary.

## CLI Entry Point

- `./.VE/bin/blackdog`

## Shipped Workflow Families

- repo lifecycle: `repo install`, `repo update`, `repo refresh`, `prompt preview`, `prompt tune`, `attempts summary`, `attempts table`
- workset/task runtime: `workset put`, `summary`, `next --workset`, `snapshot`
- supervisor execution: `supervisor start`, `supervisor show`, `supervisor checkpoint`, `supervisor release`
- same-thread task execution: `task begin`, `task show`, `task land`, `task close`, `task cleanup`
- WTAM kept-change execution: `worktree preflight`, `worktree preview`, `worktree start`, `worktree show`, `worktree land`, `worktree close`, `worktree cleanup`

## Repo Lifecycle Flow

1. `./.VE/bin/blackdog repo update --project-root .`
2. `./.VE/bin/blackdog repo refresh --project-root .`
3. `./.VE/bin/blackdog prompt preview --project-root . --prompt "..."`
4. `./.VE/bin/blackdog prompt tune --project-root . --prompt "..."`
5. review the routed docs below before editing

## Supervisor Flow

1. `./.VE/bin/blackdog supervisor start --project-root . --workset WORKSET --actor SUPERVISOR`
2. launch worker tasks from the emitted dispatch set through `task begin`
3. `./.VE/bin/blackdog supervisor show --project-root . --workset WORKSET`
4. `./.VE/bin/blackdog supervisor checkpoint --project-root . --workset WORKSET --actor SUPERVISOR --note "..."`
5. after the workset is reviewed or complete, run `./.VE/bin/blackdog supervisor release --project-root . --workset WORKSET --actor SUPERVISOR --summary "..."`

## Same-Thread Task Flow

1. `./.VE/bin/blackdog task begin --project-root . --actor AGENT --prompt "..." --prompt-mode raw`
2. make kept changes only inside the returned task worktree
3. `./.VE/bin/blackdog task land --project-root . --summary "..."`
4. if recovery is needed from that task worktree, use `./.VE/bin/blackdog task show --project-root .` or `./.VE/bin/blackdog task close --project-root . --status blocked|failed|abandoned --summary "..."`
5. if the task workspace was retained, use `./.VE/bin/blackdog task cleanup --project-root .`

## Explicit Planned-Task Flow

1. `./.VE/bin/blackdog summary --project-root .`
2. `./.VE/bin/blackdog next --project-root . --workset WORKSET`
3. `./.VE/bin/blackdog worktree preflight --project-root .`
4. `./.VE/bin/blackdog worktree preview --project-root . --workset WORKSET --task TASK --actor AGENT --prompt "..."`
5. `./.VE/bin/blackdog worktree start --project-root . --workset WORKSET --task TASK --actor AGENT --prompt "..."`
6. make kept changes only inside that task worktree
7. `./.VE/bin/blackdog worktree land --project-root . --workset WORKSET --task TASK --actor AGENT --summary "..."`

## Docs To Review

- `AGENTS.md`
- `docs/INDEX.md`
- `docs/PRODUCT_SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/TARGET_MODEL.md`
- `docs/CLI.md`
- `docs/FILE_FORMATS.md`
