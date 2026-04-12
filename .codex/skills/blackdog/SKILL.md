---
name: blackdog
description: "Use the project-local Blackdog backlog contract for Blackdog. Trigger this skill when shaping a user request into measurable backlog tasks, reviewing, adding, claiming, completing, supervising, or reporting backlog work in this repo, or when checking inbox messages and structured task results."
---

# Blackdog: Blackdog

Use the local Blackdog CLI instead of mutating backlog state by hand.

## Blackdog Layer Contract

- `blackdog_core` is the durable contract: `blackdog.toml` plus the canonical artifact files under the control root.
- `blackdog` is the shipped product surface: prompt/tune/report helpers, bootstrap/refresh flows, the generated project-local skill, the shipped static HTML board, and supervisor orchestration.
- `blackdog_cli` is the thin command adapter package behind the `blackdog` executable.
- Optional repo-specific skills, editor integrations, or wrappers should compose through documented CLI behavior and stable artifact/snapshot files rather than private Blackdog Python imports.
- Prefer CLI writes for backlog/runtime state transitions. Treat raw files as durable contracts to read and validate, not an ad hoc mutation surface.

## CLI Entry Points

- Blackdog CLI: `./.VE/bin/blackdog`
- Use `blackdog bootstrap` and `blackdog refresh` for skill scaffold setup and refresh.

## Core Paths

- Profile: `blackdog.toml`
- Control root: `@git-common/blackdog`
- Planning: `@git-common/blackdog/planning.json`
- Runtime: `@git-common/blackdog/runtime.json`
- Events: `@git-common/blackdog/events.jsonl`

## Codex Skill Discovery

- Host skill token: `blackdog`
- Skill metadata file: `.codex/skills/blackdog/SKILL.md`
- UI discovery file: `.codex/skills/blackdog/agents/openai.yaml`
- Codex discovers this skill from `agents/openai.yaml` under `.codex/skills/<skill-name>/` in the opened repo.
- `agents/openai.yaml` should explicitly mention `$blackdog` in `interface.default_prompt`.
- Open or refresh the repo in Codex after bootstrap so the skill appears in the available skill list.

## Repo-Specific Planning Guidance

- Workflow policy: Prefer the project-local blackdog CLI over hand-edited state transitions.
- Summary focus: Lead with direct status, then backlog state, then test focus.

## Rewrite Priority

Blackdog is in an explicitly authorized breaking-change period.

- Delete or narrow old code that is not being carried forward.
- Do not preserve markdown backlog, compatibility shims, or old command shapes unless they are the fastest path to the new foundation.
- Treat the WTAM kept-change workflow as the normative product path:
  - `./.VE/bin/blackdog worktree preflight`
  - `./.VE/bin/blackdog worktree start`
  - execute inside the task worktree
  - `./.VE/bin/blackdog worktree land`
  - `./.VE/bin/blackdog worktree cleanup`
- Do not preserve a non-worktree execution mode in Blackdog.
- Prioritize locking the base layer:
  - workset/task shape
  - claims attach to both worksets and tasks
  - first-class execution models: `direct_wtam` and `workset_manager`
  - durable completed/landed history
- Tests should use fresh isolated git repos. Prefer cheap/low-reasoning execution for bulk task-flow tests.


## Host Project Creation

- When the user asks to create a brand-new Blackdog repo at a filesystem path, run `./.VE/bin/blackdog create-project --project-root /abs/path --project-name "Repo Name"` from this checkout.
- `create-project` creates the target directory, initializes git, bootstraps a repo-local `.VE`, installs Blackdog from the current checkout, and runs bootstrap so the new repo already has `blackdog.toml`, `AGENTS.md`, and `.codex/skills/blackdog/`.
- Use `./.VE/bin/blackdog bootstrap` instead when the target repo already exists or already has its own Python environment prepared.

## Repo Refresh

- Run `./.VE/bin/blackdog refresh` after updating the installed Blackdog package when you want to regenerate the project-local skill files and repo-branded HTML board.
- `refresh` keeps locally modified managed files in place and writes `*.blackdog-new` sidecars with the regenerated version when a managed file has diverged.
- From a Blackdog source checkout, run `blackdog update-repo /abs/path/to/host-repo` to reinstall Blackdog into that repo's `.VE` and then run the same refresh flow.

## Standard Flow

1. Run `./.VE/bin/blackdog summary`.
2. Inspect runnable work with `./.VE/bin/blackdog next`.
3. Check the WTAM gate with `./.VE/bin/blackdog worktree preflight`.
4. Start a kept-change task with `./.VE/bin/blackdog worktree start --workset WORKSET --task TASK --actor AGENT --prompt "..."`.
5. Make kept changes only inside that task worktree.
6. Land successful kept changes with `./.VE/bin/blackdog worktree land --workset WORKSET --task TASK --actor AGENT`.
7. Clean up with `./.VE/bin/blackdog worktree cleanup --workset WORKSET --task TASK`.

## Prompt Tuning

- Use `./.VE/bin/blackdog prompt --complexity low|medium|high "..."` when you want Blackdog to rewrite a request against this repo's local docs, validation defaults, and WTAM contract before turning it into backlog work.
- `prompt` is intended to help repo-local skills that build on top of Blackdog reuse the same contract and tuning guidance instead of re-explaining the repo from scratch.
- Use `./.VE/bin/blackdog tune --no-task` when you want direct tuning guidance without automatically seeding a backlog task.

## Task Shaping

- Treat a new user request as one candidate workset-scoped deliverable first. Default to one task unless there is a measured reason to split it.
- Consolidate serial slices that touch the same files, need the same validation, or must land together. Do not create separate tasks for analysis, implementation, cleanup, and verification of the same change.
- Split only when it buys real parallelism: disjoint write sets, independent validation, separate blockers, or clearly separable deliverables that can land independently.
- Before creating or reshaping tasks, estimate total elapsed task time, active edit time, touched paths, validation time, worktree spin-ups, and coordination handoffs. Minimize separate requests first, then add parallelism only when the saved wall-clock time exceeds the extra spin-up and coordination cost.
- When uncertain, under-split first. It is easier to split a live task later than to merge redundant compatibility lanes and half-finished work.
- Use [references/task-shaping.md](references/task-shaping.md) when adding, tuning, or restructuring work; it contains the measurement fields and consolidation rubric.

## Static Board

- Static board / HTML surfaces are deferred in vNext unless explicitly rebuilt on top of `planning.json` and `runtime.json`.

## Docs to Review

Review these repo docs before editing when they apply:
  - `AGENTS.md`
  - `docs/INDEX.md`
  - `docs/PRODUCT_SPEC.md`
  - `docs/ARCHITECTURE.md`
  - `docs/TARGET_MODEL.md`
  - `docs/CLI.md`
  - `docs/FILE_FORMATS.md`

Keep `blackdog.toml` `[taxonomy].doc_routing_defaults` aligned with the repo's required review set, then regenerate this skill after routing changes.

## Supervisor Model

- Supervisor/multi-agent work is a first-class `workset_manager` execution model, but any rebuilt surface must read and write the new typed workset/runtime model directly.
- Blackdog uses branch-backed task worktrees for kept implementation changes.

## Repo Contract

- Commit `blackdog.toml` and this project-local skill if the repo wants a shared Blackdog operating contract.
- Do not check in mutable runtime files from `@git-common/blackdog`.
- Treat the documented CLI plus stable control-root artifacts as the supported integration contract for repo-local adapters and skills.
- Regenerate this skill after profile changes with `./.VE/bin/blackdog refresh`.

## Repo Defaults

- Id prefix: `BLACK`
- Buckets: core, cli, html, skills, docs, testing, integration
- Domains: cli, docs, html, state, events, inbox, results, skills
- Validation defaults:
  - `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`
