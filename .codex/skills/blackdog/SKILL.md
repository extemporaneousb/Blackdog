---
name: blackdog
description: "Use the project-local Blackdog repo contract when working on Blackdog itself. Prefer the typed vNext CLI/runtime surface over direct control-root edits."
---

# Blackdog: Blackdog

Use the local Blackdog CLI instead of mutating runtime files by hand.

## Layer Contract

- `blackdog_core` is the durable contract: profile/path resolution, typed
  planning/runtime/event formats, claim semantics, and read models.
- `blackdog` is the WTAM product layer built on that contract.
- `blackdog_cli` is the thin command adapter behind the `blackdog` executable.

## CLI Entry Point

- Blackdog CLI: `./.VE/bin/blackdog`

## Core Paths

- Profile: `blackdog.toml`
- Control root: `@git-common/blackdog`
- Planning: `@git-common/blackdog/planning.json`
- Runtime: `@git-common/blackdog/runtime.json`
- Events: `@git-common/blackdog/events.jsonl`

## Rewrite Priority

Blackdog is in an explicitly authorized breaking-change period.

- Delete or narrow old code that is not being carried forward.
- Do not preserve legacy backlog, board, bootstrap, inbox, render, or prompt
  tuning surfaces unless they are explicitly rebuilt on the vNext core model.
- Treat the WTAM kept-change workflow as the normative product path:
  - `./.VE/bin/blackdog worktree preflight`
  - `./.VE/bin/blackdog worktree start`
  - execute inside the task worktree
  - `./.VE/bin/blackdog worktree land`
  - `./.VE/bin/blackdog worktree cleanup`
- Do not preserve a non-worktree execution mode in Blackdog.
- Claims attach to both worksets and tasks.
- First-class execution models: `direct_wtam` and `workset_manager`.
- Tests should use fresh isolated git repos.

## Standard Flow

1. Run `./.VE/bin/blackdog summary`.
2. Inspect runnable work with `./.VE/bin/blackdog next`.
3. Check the WTAM gate with `./.VE/bin/blackdog worktree preflight`.
4. Start a kept-change task with `./.VE/bin/blackdog worktree start --workset WORKSET --task TASK --actor AGENT --prompt "..."`.
5. Make kept changes only inside that task worktree.
6. Land successful kept changes with `./.VE/bin/blackdog worktree land --workset WORKSET --task TASK --actor AGENT`.
7. Clean up with `./.VE/bin/blackdog worktree cleanup --workset WORKSET --task TASK`.

## Docs To Review

Review these repo docs before editing when they apply:
- `AGENTS.md`
- `docs/INDEX.md`
- `docs/PRODUCT_SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/TARGET_MODEL.md`
- `docs/CLI.md`
- `docs/FILE_FORMATS.md`

Keep `blackdog.toml` `[taxonomy].doc_routing_defaults` aligned with that set.

## Repo Contract

- Commit `blackdog.toml` and this project-local skill if the repo wants a
  shared Blackdog operating contract.
- Do not check in mutable runtime files from `@git-common/blackdog`.
- Treat the documented CLI plus stable control-root artifacts as the supported
  integration contract for repo-local adapters and skills.
