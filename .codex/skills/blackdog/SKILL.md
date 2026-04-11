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
- Backlog: `@git-common/blackdog/backlog.md`
- State: `@git-common/blackdog/backlog-state.json`
- Events: `@git-common/blackdog/events.jsonl`
- Inbox: `@git-common/blackdog/inbox.jsonl`
- Results: `@git-common/blackdog/task-results`
- HTML view: `@git-common/blackdog/blackdog-backlog.html`

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


## Host Project Creation

- When the user asks to create a brand-new Blackdog repo at a filesystem path, run `./.VE/bin/blackdog create-project --project-root /abs/path --project-name "Repo Name"` from this checkout.
- `create-project` creates the target directory, initializes git, bootstraps a repo-local `.VE`, installs Blackdog from the current checkout, and runs bootstrap so the new repo already has `blackdog.toml`, `AGENTS.md`, and `.codex/skills/blackdog/`.
- Use `./.VE/bin/blackdog bootstrap` instead when the target repo already exists or already has its own Python environment prepared.

## Repo Refresh

- Run `./.VE/bin/blackdog refresh` after updating the installed Blackdog package when you want to regenerate the project-local skill files and repo-branded HTML board.
- `refresh` keeps locally modified managed files in place and writes `*.blackdog-new` sidecars with the regenerated version when a managed file has diverged.
- From a Blackdog source checkout, run `blackdog update-repo /abs/path/to/host-repo` to reinstall Blackdog into that repo's `.VE` and then run the same refresh flow.

## Standard Flow

1. Run `./.VE/bin/blackdog validate`.
2. Run `./.VE/bin/blackdog summary`.
3. Inspect runnable work with `./.VE/bin/blackdog next`.
4. Before any repo edit you intend to keep, run `./.VE/bin/blackdog worktree preflight`. If it reports `primary worktree: yes`, do not edit in that checkout; create or enter a branch-backed task worktree with `./.VE/bin/blackdog worktree start --id TASK` first. Analysis-only work can stay in the current checkout.
5. Run `./.VE/bin/blackdog coverage --output coverage/latest.json` to collect shipping-surface validation coverage evidence before large surface edits.
6. Claim one task with `./.VE/bin/blackdog claim --agent <agent-name>`, then record structured output with `./.VE/bin/blackdog result record ...`.
7. Complete or release the task through the CLI for direct work.
8. Use `./.VE/bin/blackdog supervise run` when you want Blackdog to launch child agents instead of editing directly.
9. Check `./.VE/bin/blackdog inbox list --recipient <agent-name>` before claiming fresh work if the run may have pending instructions.
10. Open `@git-common/blackdog/blackdog-backlog.html` directly when you want the static backlog board; `blackdog render` refreshes it and active supervisor runs rerender it after task-state changes, including run exit after landed updates.

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

- `@git-common/blackdog/blackdog-backlog.html` renders a wide control board with a `Backlog Control` panel, `Status` panel, paired objective/release-gate tables, `Execution Map`, and `Completed Tasks`.
- `blackdog snapshot` and the embedded HTML payload remain Blackdog-product surfaces, but machine-readable repo/header/plan/task facts flow through the neutral `runtime_snapshot`; prefer that export for extensions instead of the surrounding board-only projection fields.
- The control panel shows the current push copy, branch/commit/run/time-on-task summary, progress bar, and plain artifact links.
- The release-gates panel stays beside the objective table and shows explicit or inferred passed checks without making the rows interactive.
- The execution map keeps only the current workset's live compatibility lanes and waves visible, carries the `Inbox JSON` link, and removes search/filter chrome.
- Objective rows are summary-only, while execution-map and completed-task cards open the task reader popout. Completed history is grouped by sweep when run metadata exists.

## Docs to Review

Review these repo docs before editing when they apply:
  - `AGENTS.md`
  - `docs/INDEX.md`
  - `docs/ARCHITECTURE.md`
  - `docs/TARGET_MODEL.md`
  - `docs/CLI.md`
  - `docs/FILE_FORMATS.md`

Keep `blackdog.toml` `[taxonomy].doc_routing_defaults` aligned with the repo's required review set, then regenerate this skill after routing changes.

## Supervisor Model

- The coordinating agent stays in the primary worktree.
- Child agents launched by `blackdog supervise ...` run in branch-backed task worktrees and land through the primary worktree after successful commits.
- Blackdog uses branch-backed task worktrees for kept implementation changes.
- `stop` messages are checked while a supervisor execution is active. Current artifacts still use `run_id` as a compatibility alias, but the steering semantics apply to the derived `WorksetExecution`.
- Tasks completed during the active execution stay visible in the execution map until the next run starts and performs its opening sweep.

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
