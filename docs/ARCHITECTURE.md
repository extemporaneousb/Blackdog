# Architecture

Blackdog vNext is organized around one durable idea: the machine-owned workset
store is the semantic source of truth.

Humans author repository docs, design docs, approvals, and prompts.
agents mutate planning and runtime state through typed Blackdog operations and
CLI surfaces. Humans can inspect the resulting files, but they are not the
preferred authoring plane.

This document is about package and storage ownership, not product workflows.
For the supported human/agent stories and the v1 target, use
[docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md).

## Package Boundaries

| Package | Role | Must not absorb |
| --- | --- | --- |
| `blackdog_core` | Durable planning/runtime contracts, typed models, and derived read models. | CLI glue, supervisor policy, HTML/view composition, or prompt-only behavior. |
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

Claims attach to both worksets and tasks. The base model supports two
first-class execution modes:

- `direct_wtam` for one kept-change task running through the WTAM lifecycle
- `workset_manager` for supervisor-led work over one claimed workset

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
2. repo lifecycle workflows over installation, refresh, and prompt/skill
   composition

The second family is intentionally not part of the workset/task durable model.
Install/update/refresh/tune are product workflows, but they are not claims,
tasks, or attempts.

## Shipped Surface After The Sweep

The minimum coherent product surface rebuilt on top of the new core is:

- `blackdog repo install`
- `blackdog repo analyze`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog workset put`
- `blackdog supervisor start`
- `blackdog supervisor show`
- `blackdog supervisor checkpoint`
- `blackdog supervisor release`
- `blackdog task begin`
- `blackdog task show`
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

The repo lifecycle family now has a shipped base in `blackdog` for
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
known legacy backlog-era artifacts from the shared control root.

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
3. claim one workset for `workset_manager`, compute dispatch candidates up to a
   parallelism cap, and checkpoint supervisor review without mutating worker
   attempts directly
4. inspect the WTAM contract before kept changes when the operator needs an
   explicit planned-task flow
5. preview one branch-backed task execution plan, including prompt receipt
   metadata, repo contract inputs, and the ordered handler plan for the task
   worktree
6. start one branch-backed task worktree with a prompt receipt, a provisioned
   worktree-local `.VE`, repo-root overlay wiring, root-bin fallback links, a
   worktree-local launcher, and real git execution identity while claiming both
   the workset and the task
7. inspect one active or latest task attempt for recovery-oriented worktree and
   claim state
8. land one successful task attempt through a canonical landed commit while
   recording structured result, validation, commit lineage, releasing claims,
   and cleaning up the task worktree by default
9. close one blocked, failed, or abandoned task attempt without landing code
10. clean up any retained or leftover task worktree
11. read summary/status
12. identify the next runnable tasks
13. emit a machine-readable runtime snapshot

## Deferred Or Removed Product Code

This repo no longer keeps legacy backlog, board, inbox, bootstrap, or
compatibility-plan code as dormant historical baggage. The rebuilt supervisor
surface now targets the new claim/runtime contract directly; legacy
backlog-era supervisor code remains removed.
