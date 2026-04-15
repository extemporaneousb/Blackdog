# Supervised Execution Target

This document defines the target Blackdog workflow for large, repo-scoped
execution that should outlive one agent thread.

It is intentionally not the shipped CLI contract today. The current shipped
surface is still:

- `blackdog supervisor start`
- `blackdog supervisor show`
- `blackdog supervisor checkpoint`
- `blackdog supervisor release`

Those commands claim one workset and expose a dispatch view. The target in this
document goes further: it compiles a high-level plan into task-scoped worker
envelopes, launches fresh worker contexts, inserts explicit supervisor review
gates, and realigns downstream work after each landed task.

## Why This Exists

Large delivery plans drift when one long-lived agent thread tries to hold:

- the full repo contract
- the whole plan
- task-level implementation detail
- prompt-tuning context
- review and recovery state

in one compacted context window.

The right model is to keep the durable plan and execution state in Blackdog,
then use a supervising agent to launch fresh worker contexts that receive only
the scoped task envelope they need.

## The Target Workflow

The target workflow should be:

1. define durable repo truth
2. compile the current goal and guardrails into one workset DAG
3. start one supervisor run over that workset
4. dispatch fresh worker agents up to a bounded parallelism cap
5. require a review-ready handoff before landing
6. land or revise each task through the supervisor
7. realign downstream task envelopes from the actual landed result
8. summarize outcomes and retire transient execution material

This keeps the repo docs, the machine-owned planning state, the worker prompt,
and the supervisor review loop as separate concerns instead of collapsing them
into one chat thread.

## Durable Vs Transient Artifacts

Blackdog should distinguish three kinds of execution material.

### Durable Repo Truth

These remain versioned in the repo because they stay true after implementation:

- `AGENTS.md`
- `docs/INDEX.md`
- architecture and product docs
- stable guardrails
- semantic-floor or migration docs that remain normative after the work lands

### Durable Machine State

These stay in the machine-owned control root:

- `planning.json`
- `runtime.json`
- `events.jsonl`

They are the durable source of truth for worksets, tasks, attempts, and audit
history.

### Transient Execution Material

These should not live in versioned repo docs by default:

- temporary delivery plans
- temporary gap lists
- review checklists that exist only to run the work
- compiled worker envelopes
- per-run supervisor summaries
- worker stdout/stderr logs
- prompt packets and prompt-tuning comparison artifacts

Unless a document remains true after the implementation lands, it should live
under the control root as a run artifact and not become permanent repo
instruction surface.

## Plan Compilation

Blackdog should treat planning as a compile step, not just an edited markdown
document.

### Inputs

The compiler inputs should be:

- the operator's high-level goal
- repo-routed docs
- selected repo-local plan or guardrail docs
- explicit non-negotiable constraints
- current `planning.json` and `runtime.json`
- prompt-tuning defaults from the repo profile

### Outputs

The compiler outputs should be:

- one workset DAG in `planning.json`
- task-scoped worker envelopes
- explicit acceptance criteria per task
- stop conditions that tell workers when to escalate instead of guessing
- downstream dependency contracts
- a promotion plan for transient docs that should become durable repo truth
- a removal plan for transient docs that should disappear after execution

The compiler output must be inspectable before execution starts. Humans should
be able to approve the compiled workset and guardrails before the supervisor
launches workers.

## Worker Envelopes

Each worker should receive only one scoped envelope, not the whole plan unless
the whole plan is necessary for that task.

A worker envelope should include:

- workset id and task id
- task objective and why it matters
- allowed paths
- required docs
- required checks
- task-specific acceptance criteria
- stop conditions
- upstream facts the task may rely on
- expected outputs needed by downstream tasks
- repo/worktree operating rules
- the exact prompt receipt Blackdog recorded for that worker

The envelope should be durable enough to restart a task in a fresh context
without reconstructing the task from chat history.

## Supervisor Run Model

The target supervisor model is a product-layer run on top of the durable
workset/task contract. It should not widen the core task runtime vocabulary
just to mirror operator workflow phases.

Core task runtime can stay small:

- `planned`
- `in_progress`
- `blocked`
- `done`

The richer workflow phases belong to the supervisor run.

### Supervisor-Owned State

One supervisor run should track:

- `run_id`
- `workset_id`
- supervisor actor
- parallelism cap
- launch transport or adapter
- active worker bindings
- review-required queue
- landing queue
- realignment checkpoints
- final summary

### Worker Binding

Blackdog should bind each task attempt to a real worker identity, not just a
generic actor label.

That binding should be adapter-aware:

- Codex app binding: agent id or session/thread identity
- CLI fallback binding: subprocess command and pid lineage

Blackdog should store the binding as product-layer run state while keeping the
core attempt model independent of any one client runtime.

## Review Gate Before Land

The target supervised flow should not auto-land on child exit.

Instead the worker should reach a review-ready state and provide:

- commit head
- changed paths
- validation results
- result summary
- residual risks
- notes for downstream tasks

The supervisor then decides one of:

- land as-is
- request revision from the same worker
- interrupt and realign the same worker
- close the current attempt and relaunch a fresh worker

This is the main quality-control boundary that prevents large-plan drift.

## Interrupt And Restart Semantics

The supervisor should be able to:

- poll active workers
- interrupt a worker when scope drifts
- send a corrected envelope or clarification
- abandon a poisoned context and relaunch a fresh worker on the same task

Attempt lineage must survive restart.

That means the runtime history should preserve:

- the prior attempt id
- the prior prompt receipt
- why the supervisor interrupted or restarted
- which later attempt superseded it

Restart is not a failure in itself. It is part of the intended control model
for long-running, high-context work.

## Downstream Realignment

After each landed task, the supervisor should refresh downstream envelopes from
actual landed state, not just the original plan.

That refresh should be able to update:

- acceptance criteria
- dependency notes
- allowed paths
- review docs
- follow-on checks

This is how the system prevents “task 5 still assumes task 2 went exactly to
plan” when the real implementation took a different but valid path.

## Prompt-Tuning Telemetry

Prompt tuning should not be limited to one same-thread direct-agent path.

The supervised target should capture enough data to compare:

- the raw user request
- the compiled plan/workset input
- the compiled worker envelope
- the worker prompt receipt
- the supervisor review disposition
- the final result status

At minimum, Blackdog should retain:

- raw request hash
- compiler version or compiler prompt hash
- worker envelope hash
- worker prompt hash
- model and reasoning settings when known
- review outcome such as `landed`, `revised`, `restarted`, or `closed`
- final task outcome and landed commit when present

That makes prompt optimization possible at the right boundary:
goal-to-workset compilation and envelope-to-worker execution.

## Product Boundary

The intended boundary is:

- `blackdog_core` owns durable planning/runtime contracts
- `blackdog` owns plan compilation, supervisor-run state, review gates, and
  worker-binding policy
- the client runtime such as Codex owns actual child-agent transport

Blackdog should not become a reimplementation of the Codex session runtime.
It should provide the repo-grounded execution contract that a Codex supervisor
can drive.

## What Blackdog Should Not Do

Blackdog should not:

- keep every temporary plan doc in the repo forever
- give every worker the whole delivery plan by default
- auto-land child work without supervisor review in the supervised model
- continue downstream tasks after upstream drift without envelope refresh
- bloat the core task runtime model with UI- or operator-specific phases

## Incremental Build Order

The practical build order should be:

1. codify this target workflow and document the durable/transient split
2. add a plan-to-workset compiler surface
3. add durable supervisor-run state and worker bindings
4. add review-ready and revise/restart semantics
5. wire the Codex transport layer for real child-agent launches
6. add prompt-tuning telemetry and comparison views for supervised runs

That sequence keeps the product grounded in repo truth and execution memory
instead of trying to rebuild the old supervisor as one large opaque runtime.
