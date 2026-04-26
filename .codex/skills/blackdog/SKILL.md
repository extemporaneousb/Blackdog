---
name: blackdog
description: "Repo-local AI development workflow for Blackdog, backed by Blackdog."
---

# Repo Skill: Blackdog

Use this repo-local skill for normal development requests. The skill is backed by the repo-local Blackdog CLI, but users do not need to name Blackdog workflow primitives in ordinary requests.
`blackdog.toml` is the machine-readable source of truth for handler setup and routed docs.

## User Workflows

- `$blackdog install or update in this repo`: before this repo-local skill exists, analyze the repo, then run `./.VE/bin/blackdog repo install --project-root .` when missing or `./.VE/bin/blackdog repo update --project-root .` followed by `./.VE/bin/blackdog repo refresh --project-root .` when already installed.
- `$blackdog do <task-description>`: build a concise execution prompt from the request and routed docs, run `./.VE/bin/blackdog task begin --project-root . --actor AGENT --prompt-file EXECUTION_PROMPT --prompt-mode skill --user-prompt-file USER_PROMPT`, make changes only in the returned task workspace, validate, then land with `./.VE/bin/blackdog task land --project-root . --summary "..."`.
- `$blackdog PM-mode <outline>`: turn the outline into planned tasks with guardrails, execute one ready slice at a time, review `summary`, `snapshot`, and `attempts summary` after each attempt, cancel superseded work, and stop when done, blocked, or user input is needed.

## Internal CLI Surface

- repo lifecycle: `repo analyze`, `repo install`, `repo update`, `repo refresh`, `attempts summary`, `attempts table`
- task execution: `task begin`, `task show`, `task recover`, `task land`, `task close`, `task cancel`, `task reopen`, `task cleanup`
- planned execution: `workset put`, `summary`, `next --workset`, `snapshot`, and the explicit `worktree` commands when low-level recovery is needed
- abandoned work is canceled by default; use `task reopen` only when it should return to normal execution

## Docs To Review

- `AGENTS.md`
- `docs/INDEX.md`
- `docs/PRODUCT_SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/TARGET_MODEL.md`
- `docs/CLI.md`
- `docs/FILE_FORMATS.md`
