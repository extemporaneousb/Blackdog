---
name: blackdog
description: "Use the repo-local Blackdog CLI and contract for Blackdog."
---

# Blackdog: Blackdog

Use the repo-local Blackdog CLI instead of mutating control-root files by hand.
The repo-local blackdog.toml handler blocks own env/runtime setup.

## CLI Entry Point

- `./.VE/bin/blackdog`

## Shipped Workflow Families

- repo lifecycle: `repo install`, `repo update`, `repo refresh`, `prompt preview`, `prompt tune`, `attempts summary`, `attempts table`
- workset/task runtime: `workset put`, `summary`, `next --workset`, `snapshot`
- WTAM kept-change execution: `worktree preflight`, `worktree preview`, `worktree start`, `worktree land`, `worktree cleanup`

## Repo Lifecycle Flow

1. `./.VE/bin/blackdog repo update --project-root .`
2. `./.VE/bin/blackdog repo refresh --project-root .`
3. `./.VE/bin/blackdog prompt preview --project-root . --prompt "..."`
4. `./.VE/bin/blackdog prompt tune --project-root . --prompt "..."`
5. review the routed docs below before editing

## WTAM Flow

1. `./.VE/bin/blackdog summary --project-root .`
2. `./.VE/bin/blackdog next --project-root . --workset WORKSET`
3. `./.VE/bin/blackdog worktree preflight --project-root .`
4. `./.VE/bin/blackdog worktree preview --project-root . --workset WORKSET --task TASK --actor AGENT --prompt "..."`
5. `./.VE/bin/blackdog worktree start --project-root . --workset WORKSET --task TASK --actor AGENT --prompt "..."`
6. make kept changes only inside that task worktree
7. `./.VE/bin/blackdog worktree land --project-root . --workset WORKSET --task TASK --actor AGENT`
8. `./.VE/bin/blackdog worktree cleanup --project-root . --workset WORKSET --task TASK`

## Docs To Review

- `AGENTS.md`
- `docs/INDEX.md`
- `docs/PRODUCT_SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/TARGET_MODEL.md`
- `docs/CLI.md`
- `docs/FILE_FORMATS.md`
