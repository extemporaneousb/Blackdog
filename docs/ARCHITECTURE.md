# Architecture

Blackdog vNext is organized around one durable idea: the machine-owned workset
store is the semantic source of truth.

Humans author repository docs, design docs, approvals, and prompts.
Agents mutate planning and runtime state through typed Blackdog operations and
CLI surfaces. Humans can inspect the resulting files, but they are not the
preferred authoring plane.

This document is about package and storage ownership, not product workflows.
For the supported human/agent stories and the v1 target, use
[docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md).

## Package Boundaries

| Package | Role | Must not absorb |
| --- | --- | --- |
| `blackdog_core` | Durable planning/runtime contracts, typed models, and derived read models. | CLI glue, orchestration policy, HTML/view composition, or prompt-only behavior. |
| `blackdog` | Product-layer WTAM orchestration and repo lifecycle workflows on top of the core contract. | Canonical planning or runtime storage ownership. |
| `blackdog_cli` | Thin parser/help/dispatch layer behind the `blackdog` executable. | Domain logic or storage semantics. |

The hard rule is unchanged: `blackdog_core` defines the contract and every
other layer consumes it.

## Durable Contract

The vNext durable contract under the control root is:

- `planning.json`
- `runtime.json`
- `events.jsonl`

`planning.json` owns the durable workset/task DAG.
`runtime.json` owns mutable task execution state, including workset claims,
task claims, prompt receipts, and worktree/git lineage for attempts.
`events.jsonl` records append-only mutations for audit and inspection.

`backlog.md` is not a storage dependency anymore.
Markdown fence parsing, raw text surgery, and plan-block compatibility logic are
gone from the semantic write path.

## Core Model

The top-level durable planning object is `Workset`.

A workset owns:

- scope
- task DAG
- visibility boundary
- policies
- canonical exported workspace identity
- branch intent for target and integration branches

Tasks remain the executable unit inside a workset, but they are no longer
grouped durably by `epic`, `lane`, or `wave`. Those concepts were structurally
wrong for the AI-first target model and were removed instead of preserved as
aliases.

Claims attach to both worksets and tasks. The shipped write path exposes one
kept-change execution model:

- `direct_wtam` for one kept-change task running through the WTAM lifecycle

Older runtime files may still load one removed managed-claim token during
migration, but that token is not part of the active runtime contract.

## Storage Boundary

`blackdog_core.backlog` exposes a planning-store interface rather than baking
JSON file operations into every semantic function. The shipped implementation is
`JsonPlanningStore`, but the semantic layer works on typed worksets and tasks.

`blackdog_core.state` does the same for runtime state through a JSON-backed
runtime store. That keeps storage substitutable without reintroducing text-based
plan editing.

## Workflow Families

Blackdog has two product-layer workflow families:

1. workset execution workflows over typed planning/runtime state
   (`workset`, `summary`, `next`, `task`, and `worktree`)
2. repo lifecycle/operator-read workflows over repo analyze/install/update/
   refresh, prompt/skill composition, and attempt inspection

The second family is intentionally not part of the workset/task durable model.
Analyze/install/update/refresh, prompt preview/tune, and attempts
summary/table are product workflows, but they are not claims, tasks, or
attempts.

Any future orchestration beyond the direct WTAM path still belongs in
`blackdog`, not in `blackdog_core`. The core model should stay small while the
product layer owns higher-level operator policy.

## Current Shipped Surface

The current coherent product surface on top of the new core is:

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
- `blackdog task recover`
- `blackdog task land`
- `blackdog task close`
- `blackdog task cleanup`
- `blackdog worktree preflight`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog worktree cleanup`
- `blackdog summary`
- `blackdog next --workset`
- `blackdog snapshot`

The `task` family is the default same-thread WTAM path.
The `worktree` family remains the explicit planned-task path when an operator
needs preflight, preview, or lower-level recovery control.

The repo lifecycle family ships in `blackdog` as
analyze/install/update/refresh, prompt preview/tune, and attempt inspection.

For repos other than Blackdog itself, `repo analyze` is the read-only
conversion entrypoint. It inventories agent docs, skills, `.VE`, launcher and
profile state, then emits findings plus a proposed conversion plan before any
repo files are mutated. `repo install` and `repo update` default to a managed
Blackdog source checkout under the control root, sourced from GitHub.
`--source-root` is the explicit local override.
When install has to create a fresh profile, it seeds routed docs from
`AGENTS.md` plus common host-repo docs that already exist, and it writes a
managed Blackdog contract block into `AGENTS.md` so WTAM rules live in repo
docs instead of only in the generated skill. `repo refresh` rewrites that
managed `AGENTS.md` block and is also the shipped cleanup path for removing
known backlog-era artifacts plus the one stale removed-orchestration run
directory from the shared control root.

Repo-local env/runtime setup is now owned by explicit handler blocks in
`blackdog.toml`, not by skill text or ad hoc bootstrap code. The shipped v1
handlers are:

- `python-overlay-venv` for the repo-root `.VE`, worktree-local overlay `.VE`,
  and root-bin fallback linking
- `blackdog-runtime` for the repo-local or worktree-local `blackdog` launcher
  plus managed-source resolution

These commands exercise one end-to-end vertical slice:

1. create or update planning and runtime state
2. start one same-thread task envelope in one command while optionally tuning
   the recorded execution prompt
3. inspect the WTAM contract before kept changes when the operator needs an
   explicit planned-task flow
4. preview one branch-backed task execution plan, including prompt receipt
   metadata, repo contract inputs, and the ordered handler plan for the task
   worktree
5. start one branch-backed task worktree with a prompt receipt, a provisioned
   worktree-local `.VE`, repo-root overlay wiring, root-bin fallback links, a
   worktree-local launcher, and real git execution identity while claiming both
   the workset and the task
6. inspect one active or latest task attempt for recovery-oriented worktree and
   claim state
7. land one successful task attempt through a canonical landed commit while
   recording structured result, validation, commit lineage, releasing claims,
   and cleaning up the task worktree by default
8. close one blocked, failed, or abandoned task attempt without landing code
9. clean up any retained or leftover task worktree
10. read summary/status
11. identify the next runnable tasks
12. emit a machine-readable runtime snapshot

## Deferred Or Removed Product Code

This repo no longer keeps legacy backlog, board, inbox, bootstrap, or
compatibility-plan code as dormant historical baggage. Legacy backlog-era
multi-agent orchestration code remains removed from the mainline repo surface.
The only remaining migration seam is cleanup of leftover removed-orchestration
control-root artifacts during `repo refresh`.
