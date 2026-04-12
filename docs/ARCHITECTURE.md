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
| `blackdog` | Product-layer WTAM orchestration on top of the core contract. | Canonical planning or runtime storage ownership. |
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

## Shipped Surface After The Sweep

The minimum coherent product surface rebuilt on top of the new core is:

- `blackdog workset put`
- `blackdog worktree preflight`
- `blackdog worktree start`
- `blackdog worktree land`
- `blackdog worktree cleanup`
- `blackdog summary`
- `blackdog next`
- `blackdog snapshot`

These commands exercise one end-to-end vertical slice:

1. create or update planning and runtime state
2. inspect the WTAM contract before kept changes
3. start one branch-backed task worktree with a prompt receipt and real git
   execution identity while claiming both the workset and the task
4. land the task branch and record structured result, validation, and commit
   lineage while releasing those claims
5. clean up the landed task worktree
6. read summary/status
7. identify the next runnable tasks
8. emit a machine-readable runtime snapshot

## Deferred Or Removed Product Code

This repo no longer keeps legacy backlog, board, inbox, bootstrap, or
compatibility-plan code as dormant historical baggage. Supervisor/workset-manager
mode is still a first-class product target, but any rebuilt supervisor surface
must target the new claim/runtime contract directly.
