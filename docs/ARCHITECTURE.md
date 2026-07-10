# Architecture

Blackdog is organized around one durable idea: the machine-owned workset store
is the semantic source of truth.

Humans author repository docs, design docs, approvals, and prompts.
Agents mutate planning and runtime state through typed Blackdog operations and
CLI surfaces. Humans can inspect the resulting files, but they are not the
preferred authoring plane.

This document owns package boundaries, storage ownership, repo lifecycle
layering, and shipped workflow ownership. CLI syntax belongs in
[docs/CLI.md](docs/CLI.md); JSON/TOML/event schemas belong in
[docs/FILE_FORMATS.md](docs/FILE_FORMATS.md).

## Package Boundaries

| Package | Role | Must not absorb |
| --- | --- | --- |
| `blackdog_core` | Durable planning/runtime contracts, typed models, and derived read models. | CLI glue, orchestration policy, HTML/view composition, or prompt-only behavior. |
| `blackdog` | Product-layer WTAM orchestration and repo lifecycle workflows on top of the core contract. | Canonical planning or runtime storage ownership. |
| `blackdog_cli` | Thin parser/help/dispatch layer behind the `blackdog` executable. | Domain logic or storage semantics. |

The hard rule is unchanged: `blackdog_core` defines the contract and every
other layer consumes it.

## Durable Contract

The durable contract under the control root is:

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

## User-Local State

Blackdog also has user-local operator/read-model state outside checked-in repo
state and outside each repo's control root. `BLACKDOG_HOME` is the explicit
override. Without it, Blackdog uses `$CODEX_HOME/blackdog` when `CODEX_HOME` is
set, otherwise `~/.codex/blackdog`.

Current user-local files are:

- `local-repos.json` for explicit local repo registry membership used by
  cross-repo stats when `--project-root` is omitted
- `codex/session-cache-v1.json` for parsed Codex session metadata used by
  Codex coverage, history, repo table, and stats read models

This state is not planning truth, runtime truth, or a repo membership authority
for scanned commands. It is cache and operator convenience state. The durable
repo contract remains `blackdog.toml` plus the control-root planning/runtime
files.

## Workflow Families

Blackdog has two product-layer workflow families:

1. workset execution workflows over typed planning/runtime state
   (`workset`, `summary`, `next`, `task`, and `worktree`)
2. repo lifecycle/operator-read workflows over repo analyze/bind/table/
   scaffold/install/update/refresh/archive/unarchive/unbind, prompt/skill
   composition, attempt inspection, user-local registry management, and stats
   reporting

The second family is intentionally not part of the workset/task durable model.
Analyze/bind/table/scaffold/install/update/refresh/archive/unarchive/unbind,
prompt preview/tune, attempts summary/table, local-repo registry commands, and
stats are product workflows, but they are not claims, tasks, or attempts.

Any future orchestration beyond the direct WTAM path still belongs in
`blackdog`, not in `blackdog_core`. The core model should stay small while the
product layer owns higher-level operator policy.

## Guardrails And Reporting

Task-class guard extension points live in the `blackdog` product layer. They
may consume `worktree preflight`, prompt preview, repo-skill context, handler
preview output, and attempt history, but they do not change the core
planning/runtime ownership boundary. The WTAM preflight surface stays focused
on workspace role, branch, dirty state, landing readiness, worktree inventory,
and the repo-local CLI path. The shipped startup guard classifies task prompts
before attempt start; deployment-class prompts must name the CI/GitHub Actions
route or explicitly approve local/emergency fallback before Blackdog creates a
new task envelope or starts an attempt. More class-specific checks for
credentials, external services, or approvals should stay layered around task
start and closeout rather than embedded into `blackdog_core`.

Environment/launcher repair expectations are owned by repo lifecycle and
handler orchestration. `repo install`, `repo update`, and `repo refresh`
validate configured handlers and repair repo-local runtime artifacts in the
repo-root scope. `worktree start` executes the handler plan for the task
workspace: worktree-local `.VE`, overlay/source-path wiring, root-bin fallback
links, and the worktree-local launcher. Handler actions and task-class guard
results are recorded as the attempt `setup_receipt` and on `worktree.start`
events when task execution starts.

Implementation-without-Blackdog detection lives in read models, not in runtime
mutation. `codex coverage` and `codex history` compare Codex session logs
against Blackdog attempts, classify implementation-like unlinked turns, attach
observed-vs-guidance environment issue evidence, and feed `repo table` and
`stats`. Those learning/report outputs support product tuning and audit without
copying full transcripts into Blackdog state.

Codex hooks and environments belong in the `blackdog` product layer. Hook
handlers may inspect preflight state, active attempts, prompt hashes, and Codex
turn metadata to provide context or guardrails, but they must not bypass the
typed planning/runtime mutation APIs. Codex environments should remain
convenience wrappers around Blackdog handlers and validation commands; they are
not the source of repo setup truth.

Supervised integration closeout is a coordination/reporting convention over
the same task-attempt model. Multiple workers can use the active Codex thread
for coordination, but durable state still flows through task begin/show/land/
close, attempt history, validation rows, residuals, follow-ups, and changed
paths. The architecture does not reintroduce a separate multi-agent runtime.

## Current Shipped Surface

The current coherent product surface on top of the new core is:

- `blackdog repo install`
- `blackdog repo analyze`
- `blackdog repo bind`
- `blackdog repo table`
- `blackdog local-repo add`
- `blackdog local-repo list`
- `blackdog local-repo remove`
- `blackdog repo scaffold`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog repo archive`
- `blackdog repo unarchive`
- `blackdog repo unbind`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog codex coverage`
- `blackdog codex history`
- `blackdog stats`
- `blackdog task begin`
- `blackdog task show`
- `blackdog task recover`
- `blackdog task land`
- `blackdog task close`
- `blackdog task cancel`
- `blackdog task reopen`
- `blackdog task cleanup`
- `blackdog worktree preflight`
- `blackdog worktree table`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog worktree cleanup`
- `blackdog summary`
- `blackdog snapshot`

The `task` family is the default same-thread WTAM path and is what generated
repo-local skills use for `$<repo-name> do ...` requests. The skill may compose
an execution prompt and pass it with `prompt-mode=skill` while recording the raw
user request as separate prompt lineage. `task begin` uses the operator's
current workspace as the routing context when that workspace belongs to the
managed Git repository, so normal linked worktrees become target branches and
receive changes only through nested task worktrees.
The `worktree` family remains the explicit low-level WTAM path when an operator
needs preflight, preview, or recovery control. Direct workset authoring remains
available only behind an explicit opt-in for planned-task migration and repair;
it is not part of the generated repo skill surface.

The repo lifecycle family ships in `blackdog` as
analyze/bind/table/scaffold/install/update/refresh/archive/unarchive/unbind,
prompt preview/tune, and attempt inspection.

For repos other than Blackdog itself, `repo analyze` is the read-only
conversion entrypoint. It inventories agent docs, skills, `.VE`, launcher and
profile state, then emits findings plus a proposed conversion plan before any
repo files are mutated. `repo bind` is the first-class membership spelling for
the install contract and wraps the same implementation as `repo install` while
reporting action `bind`. `repo install` and `repo update` default to a managed
Blackdog source checkout under the control root, sourced from GitHub.
`--source-root` is the explicit local override.
`repo scaffold` is the new-project entrypoint: it creates or reuses a target
git repo, optionally seeds starter docs from an exemplar repo, and then
delegates to `repo install` so generated project repos get the same minimal
repo-local skill and managed contract as converted repos.
When install has to create a fresh profile, it seeds routed docs from
`AGENTS.md` plus common host-repo docs that already exist, and it writes a
managed Blackdog contract block into `AGENTS.md` so WTAM rules live in repo
docs instead of only in the generated skill. `repo refresh` rewrites that
managed `AGENTS.md` block, regenerates the repo-local skill and Codex
`agents/openai.yaml` metadata, and is also the shipped cleanup path for
removing known backlog-era artifacts, obsolete Blackdog-managed skill
directories, stale generated skill auxiliary files, and the one stale
removed-orchestration run directory.
Repo lifecycle commands report when they changed repo-visible managed files.
Those changes intentionally leave the primary checkout dirty until an operator
commits, lands, reverts, or explicitly reports them, so generated skills and
managed contracts require a closing `git status --short` check.

`repo table` is a cross-repo operator-read surface. It discovers membership by
scanning supplied roots for `blackdog.toml`, deduplicates by resolved project
root, and reads each repo's runtime, attempt, and optional Codex coverage
views. Its default columns are task/attempt oriented: live state is reported
under `current_*`, historical attempt diagnostics are reported under
`window_*`, structured failure classes are counted in the window diagnostics,
Codex turn coverage includes token totals plus the longest completed single
turn for the repo, and legacy workset storage counts are hidden unless an
operator explicitly asks for the migration/debug column. It deliberately does not
read the user-local registry; `local-repo` exists for explicit operator-curated
repo sets used by stats when project roots are omitted. Optional
`[project].status` in `blackdog.toml` controls membership visibility:
missing means active, `archived` hides the repo from table output unless the
operator asks for archived rows. `repo archive` and `repo unarchive` update
only that status key. `repo unbind` is the inverse lifecycle cleanup surface:
it previews by default, strips only the managed `AGENTS.md` block on confirm,
removes Blackdog-managed profile/skill/launcher/control paths, preserves
repo-owned text and unrelated dirty files, and preserves external control dirs
outside the repo or git common dir.

`stats` is the first-class cross-repo metric read model for task, attempt, and
Codex-session counts. It accepts explicit project roots or the user-local
registry, buckets attempt and Codex turn counts by `started_at` in a selected
timezone, deduplicates exact aliases for the same resolved Blackdog project
root, reports cleanup health for terminal branch/worktree attempts, exposes
linked/unlinked Codex coverage counts, and keeps nested repos separate when
they have distinct `blackdog.toml` profiles.

Repo-local env/runtime setup is now owned by explicit handler blocks in
`blackdog.toml`, not by skill text or ad hoc bootstrap code. The shipped v1
handlers are:

- `python-overlay-venv` for the repo-root `.VE`, worktree-local overlay `.VE`,
  simple editable source path replay, and root-bin fallback linking
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
   worktree-local `.VE`, repo-root overlay wiring, worktree-local editable
   source paths, root-bin fallback links, a worktree-local launcher, and real
   git execution identity while claiming both the workset and the task
6. inspect one active or latest task attempt for recovery-oriented worktree and
   claim state, including missing branch/worktree references
7. land one successful task attempt through a canonical landed commit while
   recording structured result, validation, commit lineage, releasing claims,
   and cleaning up the task worktree by default
8. close one blocked, failed, or abandoned task attempt without landing code;
   abandoned attempts cancel the task by default
9. cancel or reopen planned work so stale tasks do not pollute normal reads
10. clean up any retained or leftover task worktree
11. read task-first summary/status, hiding canceled work unless explicitly requested
12. identify the next runnable tasks
13. emit a machine-readable task/attempt-first runtime snapshot, with legacy
    nested workset rows only on explicit request

## Deferred Or Removed Product Code

This repo no longer keeps legacy backlog, board, inbox, bootstrap, or
compatibility-plan code as dormant historical baggage. Legacy backlog-era
multi-agent orchestration code remains removed from the mainline repo surface.
The only remaining migration seam is cleanup of leftover removed-orchestration
control-root artifacts during `repo refresh`.
